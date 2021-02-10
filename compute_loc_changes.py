#!/usr/bin/env python3
import copy
import json
import multiprocessing
import operator
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import *

import dacite


@dataclass
class GitRef:
    branch: str
    _hash: str
    __verified = False

    def hash(self, git_repo) -> str:
        if not self.__verified:
            rev_parsed = subprocess.check_output(["git", "-C", str(git_repo), "rev-parse", self.branch]).decode(
                "utf-8").strip()
            if rev_parsed != self._hash:
                print("BRANCH HASH", rev_parsed, "DOES NOT MATCH EXPECTED VALUE (updates since last check?)",
                      self._hash, file=sys.stderr)
            self.__verified = True
        return self._hash


@dataclass
class ClocSummary:
    """
      "SUM": {
        "blank": 47590,
        "comment": 5545,
        "code": 132009,
        "nFiles": 334
      }
    """
    blank: int
    comment: int
    code: int
    nFiles: int


@dataclass
class ClocDiff:
    removed: ClocSummary
    added: ClocSummary
    modified: ClocSummary
    same: ClocSummary


@dataclass
class CLOCReport:
    project: "Project"
    baseline_raw: Optional[dict]
    cheri_raw: Optional[dict]
    diff_raw: Optional[dict]
    languages: Dict[str, int] = field(default_factory=dict)

    @staticmethod
    def lang_to_latex(lang):
        # Fix C++ to use macro
        return {"Assembly": "ASM", "C++": "\\cpp{}"}.get(lang, lang)

    def _parse_languages(self, data: dict):
        for lang in ("Assembly", "C", "C++"):
            if data.get(lang) is not None:
                # Ignore tiny amounts of code (e.g. nginx/openssh have a single C++ file
                loc = data[lang].get("code", 0)
                self.languages[lang] = loc

    def __post_init__(self):
        # print(self)
        if self.baseline_raw is not None:
            self._parse_languages(self.baseline_raw)
            self.baseline = dacite.from_dict(ClocSummary, self.baseline_raw["SUM"])
        if self.cheri_raw is not None:
            self.cheri = dacite.from_dict(ClocSummary, self.cheri_raw["SUM"])
        if self.diff_raw is not None:
            self.diff = dacite.from_dict(ClocDiff, self.diff_raw["SUM"])
            # Work around cases where there are some whitespace/comment/etc changes but not sloc changes
            # In that case nfiles > 0 but changes == 0, which looks confusing.
            if self.changed_loc_abs == 0 and self.changed_files_abs != 0:
                self.diff.modified.nFiles = 0
                assert self.changed_files_abs == 0, self

    @staticmethod
    def no_chages_report(name, languages: Dict[str, int], cloc_result: ClocSummary) -> "CLOCReport":
        p = Project("invalid", project_name=name, baseline=None, cheri=None,
                    extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False,
                    extra_other=False)
        result = CLOCReport(p, None, None, None)
        result.languages = languages
        result.baseline = copy.deepcopy(cloc_result)
        result.cheri = copy.deepcopy(cloc_result)
        result.diff = ClocDiff(removed=ClocSummary(0, 0, 0, 0), added=ClocSummary(0, 0, 0, 0),
                               modified=ClocSummary(0, 0, 0, 0), same=copy.deepcopy(cloc_result))
        return result

    def print_info(self):
        print("------- ", self.project.project_name, "--------------")
        print("TOTAL SLOC          ", self.baseline.code)
        print("SLOC CHANGED        ", self.changed_loc_abs)
        print("SLOC CHANGED %      ", self.changed_loc_percent)

        print("TOTAL FILES         ", self.baseline.nFiles)
        print("SLOC / FILE         ", self.baseline.code / self.baseline.nFiles)

        print("FILES CHANGED       ", self.changed_files_abs)
        print("FILES CHANGED %     ", self.changed_files_percent)
        print("-----------------------------\n")

    @property
    def changed_loc_percent(self):
        return 100.0 * (self.changed_loc_abs / self.baseline.code)

    @property
    def changed_loc_abs(self):
        return self.diff.modified.code + self.diff.added.code + self.diff.removed.code

    @property
    def changed_files_abs(self):
        # added and removed is not reliable, only look at modified files
        return self.diff.modified.nFiles

    @property
    def changed_files_percent(self):
        return 100.0 * (self.changed_files_abs / self.baseline.nFiles)

    @property
    def language_ratios(self):
        ratios = {}
        total = 0
        for _, num_loc in self.languages.items():
            total += num_loc
        for lang, num_loc in self.languages.items():
            # ignore tiny C++ test programs:
            ratio = num_loc / total
            if ratio > 0.01:
                ratios[lang] = ratio
        sorted_ratios = reversed(sorted(ratios.items(), key=operator.itemgetter(1)))
        result = OrderedDict()
        for k, v in sorted_ratios:
            result[k] = v
        return result

    @property
    def main_language(self):
        for k in self.language_ratios.keys():
            return k
        return "????"

    @property
    def languages_for_latex(self):
        if not self.languages:
            return "????"
        ratios = self.language_ratios
        if len(ratios) == 1:
            return self.lang_to_latex(ratios.popitem()[0])
        # return ", ".join("{} ({:.1f}\\%)".format(k, v * 100) for k, v in ratios.items())
        return ", ".join(self.lang_to_latex(x) for x in ratios.keys())

    @staticmethod
    def optional_str(value: Optional[Union[bool, str]]):
        if value is None:
            return "\\textcolor{red}{?}"
        if isinstance(value, str):
            return value
        if value is False:
            return ""
        if value is True:
            return "\\checkmark"
        assert False, "unreachable"

    @staticmethod
    def escape_name_for_macro(fullname: str):
        chars = []
        next_upper = True
        for c in fullname:
            if c in ("-", " ", "\t", "_", ".", "(", ")"):
                next_upper = True
                continue
            if c.isalpha():
                if next_upper:
                    chars.append(c.upper())
                    next_upper = False
                else:
                    chars.append(c)
        return ''.join(chars)

    @property
    def project_name_for_latex(self):
        return self.project.latex_project_name if self.project.latex_project_name else self.project.project_name

    def macro_definitions(self) -> str:
        fullname = self.escape_name_for_macro(self.project_name_for_latex)
        result = [
            "\\newcommand*{\\TotalSloc" + str(fullname) + "}{" + str(self.baseline.code) + "}",
            "\\newcommand*{\\ChangedSloc" + str(fullname) + "}{" + str(self.changed_loc_abs) + "}",
            "\\newcommand*{\\ChangedSlocRatio" + str(fullname) + "}{" + "{:.2f}\\%".format(
                self.changed_loc_percent) + "}",
            "\\newcommand*{\\ChangedFiles" + str(fullname) + "}{" + str(self.changed_files_abs) + "}",
            "\\newcommand*{\\ChangedFilesRatio" + str(fullname) + "}{" + "{:.1f}\\%".format(
                self.changed_files_percent) + "}"]
        return "\n".join(result) + "\n"

    def latex_row(self):
        base = "{:<35} & {:<15} & {:>8,.0f}K & {:>8,} & {:>8,} ({:.2f}\\%) & {:>8,} ({:.1f}\\%) ".format(
            self.project_name_for_latex, self.languages_for_latex,
            # report project size in kLOC
            self.baseline.code / 1000.0, self.baseline.nFiles,
            # TODO: skip changed_files_percent?
            self.changed_loc_abs, self.changed_loc_percent, self.changed_files_abs, self.changed_files_percent)
        if self.project.extra_override_text:
            assert self.project.extra_efficiency is None
            assert self.project.extra_offset is None
            assert self.project.extra_ptrcmp is None
            assert self.project.extra_cherish is None
            assert self.project.extra_other is None
            assert self.project.extra_notes is None
            xtra = " & \\multicolumn{6}{l}{" + self.project.extra_override_text + "}"
        else:
            xtra = " & {} & {} & {} & {} & {} & {}".format(
                self.optional_str(self.project.extra_efficiency),
                self.optional_str(self.project.extra_offset),
                self.optional_str(self.project.extra_ptrcmp),
                self.optional_str(self.project.extra_cherish),
                self.optional_str(self.project.extra_other),
                self.project.extra_notes or "",
            )
        return base + xtra


@dataclass
class Project:
    repo_subdir: str
    project_name: str
    baseline: Optional[GitRef]
    cheri: Optional[GitRef]
    cheri_minimal: GitRef = None
    cheri_no_offset: GitRef = None
    extra_cloc_args: List[str] = field(default_factory=list)
    commented: bool = False
    latex_project_name: Optional[str] = None
    extra_efficiency: Optional[Union[bool, str]] = None
    extra_offset: Optional[Union[bool, str]] = None
    extra_ptrcmp: Optional[Union[bool, str]] = None
    extra_cherish: Optional[Union[bool, str]] = None
    extra_other: Optional[Union[bool, str]] = None
    extra_notes: Optional[Union[bool, str]] = None
    extra_override_text: Optional[str] = None

    def run_cloc(self) -> CLOCReport:
        git_repo = repos_root / self.repo_subdir
        assert git_repo.is_dir(), git_repo
        out_json = Path(this_dir, "reports", self.project_name + ".report.json")
        diff_json_suffix = ".diff." + self.baseline.hash(git_repo) + "." + self.cheri.hash(git_repo)
        diff_json_file = out_json.with_name(out_json.name + diff_json_suffix + ".json")
        # Don't run CLOC if the diff json already exists
        run_cloc = not diff_json_file.exists()
        cloc_cmd = [str(this_dir.absolute() / "cloc/cloc"), "--include-lang=C,C++,C/C++ Header,Assembly",
                    "--out=" + str(out_json),
                    "--skip-uniqueness",  # should not be needed
                    "--processes=" + str(multiprocessing.cpu_count()),
                    "--diff-timeout", "300",  # allow up to 300 seconds per file
                    # "--diff-timeout", "0",  # infinte time per file
                    # Ignore generated files (might ignore some generators though).
                    # Hopefully most are not C/C++ (except Qt's moc)
                    "--exclude-content=\\bDO NOT EDIT\\b",
                    "--verbose=1",  # "--verbose=2",
                    "--file-encoding=UTF-8", "--json", "--git", "--count-and-diff",
                    self.baseline.hash(git_repo), self.cheri.hash(git_repo)] + self.extra_cloc_args
        if run_cloc:
            print("Running: ", " ".join(map(shlex.quote, cloc_cmd)))
            with tempfile.TemporaryDirectory() as td:
                new_env = os.environ.copy()
                if shutil.which("gtar"):
                    Path(Path(td, "tar")).symlink_to(shutil.which("gtar"))
                    new_env["PATH"] = str(td) + ":" + new_env.get("PATH", "")
                subprocess.check_call(cloc_cmd, cwd=str(git_repo), env=new_env)
        else:
            print("CLOC report found, not re-running analysis for ", self.project_name)
            print("Not running: ", " ".join(map(shlex.quote, cloc_cmd)))
            print("Delete", diff_json_file, "to force new analysis run")
        baseline_json = self._parse_json(out_json, "." + self.baseline.hash(git_repo))
        cheri_json = self._parse_json(out_json, "." + self.cheri.hash(git_repo))
        diff_json = self._parse_json(out_json, diff_json_suffix)
        result = CLOCReport(project=self, baseline_raw=baseline_json, cheri_raw=cheri_json,
                            diff_raw=diff_json)
        return result

    @staticmethod
    def _parse_json(json_base: Path, suffix) -> dict:
        json_file = json_base.with_name(json_base.name + suffix)
        final_file = json_file.with_name(json_file.name + ".json")
        if not json_file.is_file() and final_file.is_file():
            # renamed report exists, but new file doesnt
            json_file = final_file
        assert json_file.exists(), json_file
        with json_file.open("r") as jf:
            result = json.load(jf)
        # Ensure the json is sorted in the output
        with final_file.open("w") as jf:
            # print(json.dump(result, jf, sort_keys=True, ensure_ascii=False, indent=2))
            json.dump(result, jf, sort_keys=True, ensure_ascii=False, indent=2)
        if json_file != final_file:
            json_file.unlink()
        return result


def cheribsd_hybrid_subdir(name, *, extra_args, commented=False, **kwargs):
    return Project("cheribsd", project_name=name,
                   baseline=GitRef("freebsd-head-20190719", "e10b4e4c363cb013ee6c318100cc04d2bd620588"),
                   cheri=GitRef("thesis-diff", "83c181c7a1985d84312f79a5506dcc0063aeeb76"),
                   # --by-file breaks the file count in SUM...
                   # extra_cloc_args=extra_args + ["--by-file"])
                   extra_cloc_args=extra_args, commented=commented, **kwargs)


def cheribsd_purecap_subdir(name, *, extra_args, commented=False, **kwargs):
    return Project("cheribsd", project_name=name,
                   baseline=GitRef("freebsd-head-20190719", "e10b4e4c363cb013ee6c318100cc04d2bd620588"),
                   cheri=GitRef("thesis-diff-purecap", "b99dab1985e4539c0619971babeba7971d993b09"),
                   # --by-file breaks the file count in SUM...
                   # extra_cloc_args=extra_args + ["--by-file"])
                   extra_cloc_args=extra_args, commented=commented, **kwargs)


this_dir = Path(__file__).parent.absolute()
local_scratch = Path("/local/scratch/alr48/cheri")
if local_scratch.is_dir():
    repos_root = local_scratch
else:
    repos_root = Path.home() / "cheri"

# WARNING: can't have a trailing / in the --match-d= option. Need to use (/|$)?
projects = [
    Project("nginx", project_name="NGINX",
            baseline=GitRef("baseline", "ff16c6f99c6cc0959d1632fb4030730ba27657ef"),
            cheri=GitRef("master", "d5794c5167f10e2230078dd798e4033beb1b1b6b"),
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=False,
            extra_notes="$\\approx$~50\\% changes non-essential"),
    Project("postgres", project_name="PostgreSQL",
            baseline=GitRef("baseline", "5329606693fcd132882c284abbb66bd296a24549"),
            cheri=GitRef("96-cheri", "83f3bec1f43a7dc92e95f273d49748dc131c567e"),
            extra_efficiency=False, extra_offset=True, extra_ptrcmp=False, extra_cherish=False, extra_other=True,
            extra_notes="\\textgreater~50\\% changes non-essential"),
    Project("sqlite", project_name="SQLite",
            baseline=GitRef("baseline", "6cbb084d16693e3ce8ea0bcfd96520abc5c3a886"),
            cheri=GitRef("branch-3.19", "41e4b8906c1480487847f1ac8515b1573c3b22f8"),
            extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False, extra_other=False),
    # Project("qt5/qtbase", project_name="QtBase",
    #        baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
    #        cheri=GitRef("5.10", "33de53d5ec4c3f58b4960e835911215388e38235"),
    #        # Ignore the giant sqlite.c file since it will time out
    #        extra_cloc_args=["--not-match-f=/src/3rdparty/sqlite/sqlite3.c"]),
    Project("qt5/qtbase", project_name="QtBase (excluding tests)", latex_project_name="QtBase",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10", "33de53d5ec4c3f58b4960e835911215388e38235"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(src|include)(/|$)", "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=True),
    Project("rsync", project_name="rsync",
            baseline=GitRef("baseline", "c0c6a97c35e8e4fb56ba26dc9c8447e26d94de06"),
            cheri=GitRef("master", "ae924454e0857298515f044a363895687b9bcdf9"),
            cheri_minimal=GitRef("cheri-minimal", "00000000000000"),
            extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False, extra_other=True,
            extra_notes="Only required change is a bug-fix"),
    Project("llvm-project", project_name="libc++ (excluding tests)", latex_project_name="\\libcxx (lib only)",
            baseline=GitRef("latest-merge", "6b56ad164cedab90a9b79bfd189a1a27622a24fa"),
            cheri=GitRef("master", "82040a73128e2dbb8de98a14639c6b10975d8165"),
            # Need to also count the files without extensions
            extra_cloc_args=["--match-d=/libcxx/(src|include|lib)(/|$)", "--lang-no-ext=C/C++ Header"],
            extra_notes="\\textgreater~20\\% changes for \\name",
            extra_efficiency=False, extra_offset=True, extra_ptrcmp=False, extra_cherish=True, extra_other=False),
    Project("llvm-project", project_name="libc++ (full)", latex_project_name="\\libcxx (full)",
            baseline=GitRef("latest-merge", "6b56ad164cedab90a9b79bfd189a1a27622a24fa"),
            cheri=GitRef("master", "82040a73128e2dbb8de98a14639c6b10975d8165"),
            commented=True,
            # Need to also count the files without extensions
            extra_cloc_args=["--match-d=/libcxx/", "--lang-no-ext=C/C++ Header"],
            extra_notes="\\textgreater~60\\% test changes non-essential", extra_efficiency=False, extra_offset=True,
            extra_ptrcmp=False, extra_cherish=True, extra_other=True),
    Project("llvm-project", project_name="libc++ (test suite)", latex_project_name="\\libcxx (test suite)",
            baseline=GitRef("latest-merge", "6b56ad164cedab90a9b79bfd189a1a27622a24fa"),
            cheri=GitRef("master", "82040a73128e2dbb8de98a14639c6b10975d8165"),
            # Need to also count the files without extensions
            extra_cloc_args=["--match-d=/libcxx/(test)(/|$)", "--lang-no-ext=C/C++ Header"],
            extra_notes="\\textgreater~60\\% changes non-essential", extra_efficiency=False, extra_offset=True,
            extra_ptrcmp=False, extra_cherish=True, extra_other=True),
    Project("llvm-project", project_name="compiler-rt (excluding tests)", latex_project_name="compiler-rt",
            baseline=GitRef("latest-merge", "6b56ad164cedab90a9b79bfd189a1a27622a24fa"),
            cheri=GitRef("master", "82040a73128e2dbb8de98a14639c6b10975d8165"),
            extra_cloc_args=["--match-d=/compiler-rt/(src|include|lib)(/|$)"],
            extra_efficiency=True, extra_offset=False, extra_ptrcmp=True, extra_cherish=False, extra_other=False),
    Project("qt5/qtwebkit", project_name="QtWebkit",
            baseline=GitRef("baseline", "4ce8ebc4094512b9916bfa5984065e95ac97c9d8"),
            cheri=GitRef("qtwebkit-5.212-cheri", "d6854ceb1cc52a1838316067b41e93f3dc83c2f7"),
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False,
            extra_other=True, extra_notes="Many changes for split register file"),
    Project("icu4c", project_name="ICU4C",
            baseline=GitRef("baseline", "9e93ceca26803122e05da9725721a16ad13c190f"),
            cheri=GitRef("master", "9c39ecaf34dc0e3dd4f2bbec474e4ce190473017"),
            extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False, extra_other=True,
            extra_notes="No CHERI-specific changes"),
    cheribsd_hybrid_subdir("FreeBSD libc",
                           extra_args=[
                               "--match-d=/((lib/libc)|(contrib/libc-vis)|(contrib/tzcode/stdtime)|(contrib/gdtoa)|(contrib/jemalloc))(/|$)",
                               # exclude massive generated jemalloc header
                               "--not-match-f=size_classes.h"],
                           extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=True,
                           extra_other=False),
    cheribsd_hybrid_subdir("OpenSSH", extra_args=["--match-d=/crypto/openssh(/|$)"],
                           extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False,
                           extra_other=False),
    cheribsd_hybrid_subdir("OpenSSL", extra_args=["--match-d=/crypto/openssl(/|$)"],
                           extra_efficiency=False, extra_offset=True, extra_ptrcmp=False, extra_cherish=True,
                           extra_other=False),
    cheribsd_hybrid_subdir("FreeBSD kernel", latex_project_name="FreeBSD kernel (hybrid, all files)", commented=True,
                           extra_args=["--match-d=/sys(/|$)", "--not-match-d=test", "--not-match-f=pmap_mips64.c"],
                           extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False,
                           extra_other=True),

    # Generate a full FreeBSD kernel count to analyze files with most changes
    cheribsd_hybrid_subdir("FreeBSD kernel (full-by-file)",
                           extra_args=["--match-d=/sys(/|$)", "--by-file", "--not-match-f=pmap_mips64.c",
                                       "--not-match-d=test"], commented=True),
    cheribsd_hybrid_subdir("FreeBSD kernel (no drivers)", latex_project_name="FreeBSD kernel (hybrid)",
                           extra_args=["--match-d=/sys(/|$)", "--not-match-f=pmap_mips64.c", "--not-match-d=test",
                                       "--exclude-dir=dev"],
                           extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False,
                           extra_other=True),
    # Purecap kernel:
    cheribsd_purecap_subdir("purecap kernel", latex_project_name="FreeBSD kernel (pure, all files)", commented=True,
                            extra_args=["--match-d=/sys(/|$)", "--not-match-d=test", "--not-match-f=pmap_mips64.c"],
                            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=True,
                            extra_other=True),
    cheribsd_purecap_subdir("purecap kernel (no drivers)", latex_project_name="FreeBSD kernel (pure)",
                            extra_args=["--match-d=/sys(/|$)", "--not-match-f=pmap_mips64.c", "--not-match-d=test",
                                        "--exclude-dir=dev"], extra_efficiency=True, extra_offset=True,
                            extra_ptrcmp=True, extra_cherish=True, extra_other=True),
    # libxml2 does not need any changes other than alloc-size for precision report
    Project("libxml2", project_name="libxml2", commented=True, latex_project_name="libxml2 (including alloc_size)",
            baseline=GitRef("baseline", "030b1f7a27c22f9237eddca49ec5e620b6258d7d"),
            # cheri=GitRef("cheri-baseline", "030b1f7a27c22f9237eddca49ec5e620b6258d7d")),
            cheri=GitRef("master", "a7c68cd6e7ddacba2081bc58e0db90d348ea4830"),
            extra_efficiency=None, extra_offset=None, extra_ptrcmp=None, extra_cherish=False, extra_other=True),
]
reports = []
for project in projects:
    reports.append(project.run_cloc())

reports.append(CLOCReport.no_chages_report("SPECINT2006", {"C": 218398, "C++": 25606},
                                           ClocSummary(blank=47386, comment=49755, code=258461, nFiles=466)))
reports.append(CLOCReport.no_chages_report("libxml2", {"C": 100},
                                           ClocSummary(blank=29407, comment=57482, code=231071, nFiles=184)))
for report in reports:
    report.print_info()

# sort by number of changes, and if that's equal by number of SLOC
reports.sort(key=operator.attrgetter('main_language', 'changed_loc_percent', 'changed_loc_abs'))

latex = """
\\begin{table}[]
\\centering
\\begin{tabular}{@{}l|rr|rr@{}}
\\FL
                      & \\multicolumn{2}{c|}{Total counts} & \\multicolumn{2}{c}{CHERI changes} \\NN
Project                 & \\multicolumn{1}{c}{SLOC}   & \\multicolumn{1}{c|}{files}  & \\multicolumn{1}{c}{SLOC} &  \\multicolumn{1}{c}{files} \\ML
"""
table_body = ""

for i, report in enumerate(reports):
    if report.project.commented:
        table_body += "% "
    table_body += report.latex_row()
    if i != len(reports) - 1:
        table_body += "\\NN\n"

table_body += "\\LL\n"
latex += table_body
latex += """
\\end{tabular}
\\caption{FOO}
\\label{tab:cheri-compat-changes}
\\end{table}
"""

# Save the table
print(latex)
Path(this_dir / "table-data-rows.tex").write_text(table_body)
Path(this_dir / "table.tex").write_text(latex)

print("DONE")

macro_defs = ""
ensure_unique_project_names = set()  # avoid latex errors
for i, report in enumerate(reports):
    macro_defs += report.macro_definitions()
    assert report.project_name_for_latex not in ensure_unique_project_names
    ensure_unique_project_names.add(report.project_name_for_latex)

worst_report = max(reports, key=operator.attrgetter("changed_loc_percent"))
print(worst_report)
macro_defs += "\n\n\\newcommand*{\\ChangedSlocMax}{" + str(worst_report.changed_loc_abs) + "}\n"
macro_defs += "\\newcommand*{\\ChangedSlocMaxRatio}{" + "{:.2f}\\%".format(worst_report.changed_loc_percent) + "}\n"
macro_defs += "\\newcommand*{\\ChangedSlocMaxProject}{" + str(worst_report.project_name_for_latex) + "}\n"

print(macro_defs)
Path(this_dir / "changes-macros.tex").write_text(macro_defs)
