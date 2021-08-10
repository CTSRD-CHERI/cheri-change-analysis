#!/usr/bin/env python3
import argparse
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
import hashlib
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

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
                print("BRANCH HASH", rev_parsed, "FOR", self.branch,
                      "DOES NOT MATCH EXPECTED VALUE (updates since last check?)", self._hash, file=sys.stderr)
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
    project: "ProjectBase"
    baseline_raw: dict
    cheri_raw: Optional[dict] = None
    cheri: Optional[ClocSummary] = None
    diff_raw: Optional[dict] = None
    diff: Optional[ClocDiff] = None
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
    def no_changes_report(name, languages: Dict[str, int], cloc_result: ClocSummary) -> "CLOCReport":
        p = Project("invalid", project_name=name, baseline=None, cheri=None,
                    extra_efficiency=False, extra_offset=False, extra_ptrcmp=False, extra_cherish=False,
                    extra_other=False)
        result = CLOCReport(p, dict(), None, None)
        result.languages = languages
        result.baseline = copy.deepcopy(cloc_result)
        result.cheri = copy.deepcopy(cloc_result)
        result.diff = ClocDiff(removed=ClocSummary(0, 0, 0, 0), added=ClocSummary(0, 0, 0, 0),
                               modified=ClocSummary(0, 0, 0, 0), same=copy.deepcopy(cloc_result))
        return result

    def print_info(self):
        print("------- ", self.project.project_name, "--------------")
        print("Languages          ", " ".join(f"{v*100:.2f}% {k}" for k, v in self.language_ratios.items()))
        print(f"TOTAL SLOC          {self.baseline.code:,}")
        print(f"SLOC CHANGED        {self.changed_loc_abs:,}")
        print(f"SLOC CHANGED %      {self.changed_loc_percent:,}")

        print(f"TOTAL FILES         {self.baseline.nFiles:,}")
        print(f"SLOC / FILE         {self.baseline.code / self.baseline.nFiles:,}")
        print(f"FILES CHANGED       {self.changed_files_abs:,}")
        print(f"FILES CHANGED %     {self.changed_files_percent:,}")
        print("-----------------------------\n")

    @property
    def changed_loc_percent(self):
        return 100.0 * (self.changed_loc_abs / self.baseline.code)

    @property
    def changed_loc_abs(self):
        if self.diff is None:
            return 0
        return self.diff.modified.code + self.diff.added.code + self.diff.removed.code

    @property
    def changed_files_abs(self):
        if self.diff is None:
            return 0
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
        if not isinstance(self.project, Project):
            xtra = " & & & & & &"
        else:
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


class ProjectBase:
    project_name: str
    commented: bool
    no_cheri_specific_changes: bool

    @property
    def no_cheri_specific_changes(self) -> bool:
        raise NotImplementedError()

    def run_cloc(self) -> CLOCReport:
        raise NotImplementedError()

    @staticmethod
    def _parse_json(json_base: Path, suffix: Optional[str], optional: bool = False) -> Optional[dict]:
        if suffix is None:
            json_file = json_base
            final_file = json_base
        else:
            json_file = json_base.with_name(json_base.name + suffix)
            final_file = json_file.with_name(json_file.name + ".json")
        if not json_file.is_file() and final_file.is_file():
            # renamed report exists, but new file doesnt
            json_file = final_file
        if optional and not json_file.exists():
            return None
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

@dataclass
class Project(ProjectBase):
    repo_subdir: str
    project_name: str
    baseline: GitRef
    cheri: GitRef
    cheri_minimal: Optional[GitRef] = None
    cheri_no_offset: Optional[GitRef] = None
    extra_cloc_args: List[str] = field(default_factory=list)
    commented: bool = False
    no_cheri_specific_changes: bool = False
    latex_project_name: Optional[str] = None
    extra_efficiency: Optional[Union[bool, str]] = None
    extra_offset: Optional[Union[bool, str]] = None
    extra_ptrcmp: Optional[Union[bool, str]] = None
    extra_cherish: Optional[Union[bool, str]] = None
    extra_other: Optional[Union[bool, str]] = None
    extra_notes: Optional[Union[bool, str]] = None
    extra_override_text: Optional[str] = None

    def run_cloc(self) -> CLOCReport:
        git_repo = args.source_root / self.repo_subdir
        assert git_repo.is_dir(), git_repo
        out_json = Path(this_dir, "reports", self.project_name + ".report")
        diff_json_suffix = ".diff." + self.baseline.hash(git_repo) + "." + self.cheri.hash(git_repo)
        diff_json_file = out_json.with_name(out_json.name + diff_json_suffix + ".json")
        baseline_json_file = out_json.with_name(out_json.name + "." + self.baseline.hash(git_repo) + ".json")
        cheri_json_file = out_json.with_name(out_json.name + "." + self.cheri.hash(git_repo) + ".json")
        # Don't run CLOC if the diff json already exists (or if there is no diff and both others exist
        skip_cloc = diff_json_file.exists() or (baseline_json_file.exists() and cheri_json_file.exists())
        cloc_cmd = [args.cloc,
                    "--include-lang=C,C++,C/C++ Header,Assembly",
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
        if not skip_cloc:
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
        diff_json = self._parse_json(out_json, diff_json_suffix, optional=True)
        result = CLOCReport(project=self, baseline_raw=baseline_json, cheri_raw=cheri_json,
                            diff_raw=diff_json)
        return result

@dataclass
class UnmodifiedProject(ProjectBase):
    repo_subdir: str
    project_name: str
    baseline: GitRef
    extra_cloc_args: List[str] = field(default_factory=list)
    commented: bool = False
    latex_project_name: Optional[str] = None

    @property
    def no_cheri_specific_changes(self):
        return True

    def run_cloc(self) -> CLOCReport:
        git_repo = args.source_root / self.repo_subdir
        assert git_repo.is_dir(), git_repo
        basename_json = Path(this_dir, "reports", self.project_name + ".report")
        result_json = Path(basename_json.parent, basename_json.name + "." + self.baseline.hash(git_repo) + ".json")
        # Don't run CLOC if the json already exists
        run_cloc = not result_json.exists()
        cloc_cmd = [args.cloc,
                    "--include-lang=C,C++,C/C++ Header,Assembly",
                    "--out=" + str(result_json),
                    "--processes=" + str(multiprocessing.cpu_count()),
                    # Ignore generated files (might ignore some generators though).
                    # Hopefully most are not C/C++ (except Qt's moc)
                    "--exclude-content=\\bDO NOT EDIT\\b",
                    "--verbose=1",  # "--verbose=2",
                    "--file-encoding=UTF-8", "--json", "--git",
                    self.baseline.hash(git_repo)] + self.extra_cloc_args
        if run_cloc:
            print("Running: ", " ".join(map(shlex.quote, cloc_cmd)))
            subprocess.check_call(cloc_cmd, cwd=str(git_repo))
        else:
            print("CLOC report found, not re-running analysis for ", self.project_name)
            print("Not running: ", " ".join(map(shlex.quote, cloc_cmd)))
            print("Delete", result_json, "to force new analysis run")
        baseline_json = self._parse_json(basename_json, "." + self.baseline.hash(git_repo))
        assert baseline_json is not None
        result = CLOCReport(project=self, baseline_raw=baseline_json)
        return result

@dataclass
class UnmodifiedDirectories(ProjectBase):
    directories: List[str]
    project_name: str
    base_directory: Optional[str] = None
    extra_cloc_args: List[str] = field(default_factory=list)
    commented: bool = False
    latex_project_name: Optional[str] = None

    @property
    def no_cheri_specific_changes(self):
        return True

    def run_cloc(self) -> CLOCReport:
        # Add a suffix so that we re-run analysis if the list of dirs changes
        all_dirs = "".join(sorted(self.directories))
        report_suffix = hashlib.sha1(all_dirs.encode("utf-8")).hexdigest()
        result_json = Path(this_dir, "reports", f"{self.project_name}.report.{report_suffix}.json")
        # Don't run CLOC if the json already exists
        run_cloc = not result_json.exists()
        cloc_cmd = [args.cloc,
                    "--include-lang=C,C++,C/C++ Header,Assembly",
                    "--out=" + str(result_json),
                    "--processes=" + str(multiprocessing.cpu_count()),
                    # Ignore generated files (might ignore some generators though).
                    # Hopefully most are not C/C++ (except Qt's moc)
                    "--exclude-content=\\bDO NOT EDIT\\b",
                    "--verbose=1",  # "--verbose=2",
                    "--file-encoding=UTF-8", "--json"] + self.extra_cloc_args + self.directories
        if run_cloc:
            print("Running: ", " ".join(map(shlex.quote, cloc_cmd)))
            cwd = args.source_root if self.base_directory is None else args.source_root / self.base_directory
            subprocess.check_call(cloc_cmd, cwd=str(cwd))
        else:
            print("CLOC report found, not re-running analysis for ", self.project_name)
            print("Not running: ", " ".join(map(shlex.quote, cloc_cmd)))
            print("Delete", result_json, "to force new analysis run")
        baseline_json = self._parse_json(result_json, None)
        assert baseline_json is not None
        result = CLOCReport(project=self, baseline_raw=baseline_json)
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


def default_repos_root() -> Path:
    local_scratch = Path("/local/scratch/alr48/cheri")
    if local_scratch.is_dir():
        return local_scratch
    else:
        return Path.home() / "cheri"

parser = argparse.ArgumentParser()
parser.add_argument("--verbose", action="store_true")
parser.add_argument("--cloc", default=str(this_dir.absolute() / "cloc/cloc"))
parser.add_argument("--source-root", type=lambda x: Path(x).absolute(), default=default_repos_root())
args = parser.parse_args()

# WARNING: can't have a trailing / in the --match-d= option. Need to use (/|$)?

# region  Data for https://www.cl.cam.ac.uk/techreports/UCAM-CL-TR-949.html
thesis_projects = [
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
    #        cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
    #        # Ignore the giant sqlite.c file since it will time out
    #        extra_cloc_args=["--not-match-f=/src/3rdparty/sqlite/sqlite3.c"]),
    Project("qt5/qtbase", project_name="QtBase (excluding tests)", latex_project_name="QtBase",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
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
# endregion

# Enter you projects here:
# projects: List[ProjectBase] = [
#     Project("nginx", project_name="NGINX",
#             baseline=GitRef("baseline", "ff16c6f99c6cc0959d1632fb4030730ba27657ef"),
#             cheri=GitRef("master", "d5794c5167f10e2230078dd798e4033beb1b1b6b"),
#             extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=False,
#             extra_notes="$\\approx$~50\\% changes non-essential"),
# ]

# region  Data for DSbD report
dsbd_non_total_count_projects: List[ProjectBase] = [
    Project("qt5/qtbase", project_name="QtBase 5.15 (everything)",
            baseline=GitRef("baseline-5.15", "970c51ec4861f20ebb33f5299298857669c92aad"),
            cheri=GitRef("5.15", "d9424709b6be50aa093d9d021cd126bd6570ec96"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_notes="Lots of changes"),

    Project("qt5/qtbase", project_name="QtBase 5.10 (src only)",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(src|include)(/|$)", "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=True),
    Project("qt5/qtbase", project_name="QtBase 5.10 (tests only)",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(tests)(/|$)",
                             "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=True),
    Project("qt5/qtbase", project_name="QtBase 5.10 (examples only)",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(examples)(/|$)",
                             "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=True),
    Project("qt5/qtbase", project_name="QtBase 5.10 (everything)",
            baseline=GitRef("upstream/5.10", "4ba535616b8d3dfda7fbe162c6513f3008c1077a"),
            cheri=GitRef("5.10-thesis", "33de53d5ec4c3f58b4960e835911215388e38235"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_efficiency=True, extra_offset=True, extra_ptrcmp=True, extra_cherish=False, extra_other=True),
    Project("xvnc-server", project_name="XVnc server (all)",
            baseline=GitRef("xorg-server-1.20.12", "5e516c7be478eb66088e9898407202b07ba8c790"),
            cheri=GitRef("server-1.20-branch", "1250bc8fdb1ecb1b94e29c32e3d15403ee0c64fc"),
            extra_cloc_args=[],
            extra_notes="Fix realloc"),
]
for p in dsbd_non_total_count_projects:
    p.commented = True

dsbd_projects: List[ProjectBase] = [
    Project("qt5/qtsvg", project_name="QtSvg",
            baseline=GitRef("baseline", "aceea78cc05ac8ff947cee9de8149b48771781a8"),
            cheri=GitRef("5.15", "05a9d31286044c18acbc93cd996e7db23ff3cff6"),
            extra_notes="Fix out-of-bounds read for empty strings"),
    Project("qt5/qtdeclarative", project_name="QtDeclarative",
            baseline=GitRef("baseline", "6683c414c5cc6ab46197c41bb1361c518ca84d3e"),
            cheri=GitRef("5.15", "f968686b677a07728173985043ec0c6c0a2a1485"),
            extra_notes="Lots of changes"),
    Project("qt5/qtgraphicaleffects", project_name="QtGraphicalEffects",
            baseline=GitRef("baseline", "c36998dc1581167b12cc3de8e4ac68c2a5d9f76e"),
            cheri=GitRef("5.15", "7dffbb886337c3527956f3ff32e35ab2e9979aa0"),
            extra_notes="Lots of changes", no_cheri_specific_changes=True),

    Project("qt5/qtbase", project_name="QtBase 5.15 (src only)",
            baseline=GitRef("baseline-5.15", "970c51ec4861f20ebb33f5299298857669c92aad"),
            cheri=GitRef("5.15", "d9424709b6be50aa093d9d021cd126bd6570ec96"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(src|include)(/|$)", "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_notes="Lots of changes"),
    Project("qt5/qtbase", project_name="QtBase 5.15 (tests only)",
            baseline=GitRef("baseline-5.15", "970c51ec4861f20ebb33f5299298857669c92aad"),
            cheri=GitRef("5.15", "d9424709b6be50aa093d9d021cd126bd6570ec96"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(tests)(/|$)",
                             "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_notes="Lots of changes"),
    Project("qt5/qtbase", project_name="QtBase 5.15 (examples only)",
            baseline=GitRef("baseline-5.15", "970c51ec4861f20ebb33f5299298857669c92aad"),
            cheri=GitRef("5.15", "d9424709b6be50aa093d9d021cd126bd6570ec96"),
            # Ignore the giant sqlite.c file since it will time out
            extra_cloc_args=["--match-d=/(examples)(/|$)",
                             "--not-match-f=/src/3rdparty/sqlite/sqlite3.c"],
            extra_notes="Lots of changes"),

    UnmodifiedProject("tigervnc", project_name="TigerVNC",
                      baseline=GitRef("master", "dccb95f345f7a9c5aa785a19d1bfa3fdecd8f8e0")),
    Project("xvnc-server", project_name="XVnc server",
            baseline=GitRef("xorg-server-1.20.12", "5e516c7be478eb66088e9898407202b07ba8c790"),
            cheri=GitRef("server-1.20-branch", "1250bc8fdb1ecb1b94e29c32e3d15403ee0c64fc"),
            extra_cloc_args=["--fullpath", "--not-match-d=/hw/.*"],
            extra_notes="Fix realloc"),

    Project("libxfont", project_name="LibXFont",
            baseline=GitRef("baseline", "ce7a3265019e4d66198c1581d9e8c859c34e8ef1"),
            cheri=GitRef("master", "daff8876379c64c7bee126319af804896f83b5da"),
            extra_notes="Fix OOB read"),

    Project("xorgproto", project_name="xorgproto",
            baseline=GitRef("xorgproto-2021.4.99.2", "47cc19608e6dde565296ed46839105663eae772f"), # "9cd746bd0d5c23f0929342cb3cbe17f0c8407d37"),
            cheri=GitRef("master", "a0ed054ee2c334941dfe9eaa7bcfdbbe6907e1b5"),
            extra_notes="Fix 64-bit long detection",
            extra_cloc_args=["--force-lang=C,h"]),  # only contains C headers
    Project("libx11", project_name="LibX11",
            baseline=GitRef("baseline", "401f58f8ba258d4e7ce56a8f756595b72e544c15"),
            cheri=GitRef("my-fdo-fork/fix-realloc-ub", "d01d23374107f6fc55511f02559cf75be7bdf448"),
            extra_notes="Fix 64-bit long detection and realloc abuse"),
    Project("libxt", project_name="LibXt",
            baseline=GitRef("libXt-1.2.1", "edd70bdfbbd16247e3d9564ca51d864f82626eb7"),
            cheri=GitRef("master", "1d5bb760ee996927dd5dfa5b3c219b3d6ef63d11"),
            extra_notes="fix long detection and Fix long vs pointer"),


    # Desktop
    Project("kde-frameworks/kwin", project_name="KWin (security fix)",
            baseline=GitRef("mykde/master", "2ba13f4a089b4ab4d833a8d1fbb7e05cf5b52ee0"),
            cheri=GitRef("master", "00b832a19ef10f8050cf1bf4144b17e514f457b7"),
            extra_notes="fix long detection and Fix long vs pointer"),
    Project("kde-frameworks/kwin", project_name="KWin (build system + optional)",
            baseline=GitRef("baseline", "ed57ac39e2ac98aee56e4f44789e3199df11a117"),
            cheri=GitRef("mykde/master", "2ba13f4a089b4ab4d833a8d1fbb7e05cf5b52ee0"),
            extra_notes="fix long detection and Fix long vs pointer", commented=True),

    Project("kde-frameworks/plasma-framework", project_name="Plasma-framework",
            baseline=GitRef("upstream/master", "75c31c08d560d51fcdeba2dc3d54e5c9d31fb3ca"),
            cheri=GitRef("mykde/master", "eea1c51ab140c193d8e4da8f0347f7d9bbee3dae"),
            extra_notes="misc optional deps", no_cheri_specific_changes=True),
    Project("kde-frameworks/plasma-workspace", project_name="Plasma-workspace",
            baseline=GitRef("upstream/master", "270fe778fabc656e58d287e6b1221a3755e54106"),
            cheri=GitRef("mykde/master", "3c8f68f43086e919b65204d00fafd90381481197"),
            extra_notes="misc", no_cheri_specific_changes=True),
    Project("kde-frameworks/plasma-desktop", project_name="Plasma-desktop",
            baseline=GitRef("upstream/master", "4dd957eb2d00fc9b6bea803c2997af409c7cb379"),
            cheri=GitRef("mykde/master", "307ee111ca94d62649222626b2b0a9171e14eb84"),
            extra_notes="misc", no_cheri_specific_changes=True),
    # Apps
    Project("kde-frameworks/dolphin", project_name="Dolphin",
            baseline=GitRef("baseline", "d284e22f8730e98336fab515a339143341f55ec1"),
            cheri=GitRef("dbus-fix", "3fdd93db97bab9ca15e65047d69774cfbfe22f27"),
            extra_notes="dbus", no_cheri_specific_changes=True),
    Project("kde-frameworks/gwenview", project_name="Gwenview",
            baseline=GitRef("baseline", "a4f13057a0bcf189a3249f2ec8d6ca5a5bfb1a0f"),
            cheri=GitRef("optional-deps", "4128e0baf993a154edeba7c8491684818ce039cc"),
            extra_notes="dbus and opengl optional", no_cheri_specific_changes=True),
    Project("kde-frameworks/okular", project_name="Okular",
            baseline=GitRef("baseline", "21bc8bd023eee97dc7fbb34955488e0cf6214c04"),
            cheri=GitRef("optional-deps", "3b3dc10a712683c7a27bc0cd3d64dee7dec0a2cc"),
            extra_notes="dbus optional", no_cheri_specific_changes=True),
    Project("kde-frameworks/systemsettings", project_name="Systemsettings",
            baseline=GitRef("origin/master", "6047a73514ab75037d5217624fd31e0ee2ea79d8"),
            cheri=GitRef("mykde/master", "dfda380f08abdf4fa67e4b1eb4f1627e7ee3ff68"),
            extra_notes="dbus optional", no_cheri_specific_changes=True),

    Project("poppler", project_name="Poppler",
            baseline=GitRef("baseline", "f35567dc6033cf8f856f5694af058fda2528cbe7"),
            cheri=GitRef("master", "cc1807002f038787de53a81128ab46a5d96ea759"),
            extra_notes="Silence warning", no_cheri_specific_changes=True),
    Project("fontconfig", project_name="fontconfig",
            baseline=GitRef("my-fdo-fork/baseline", "3a7ad1b49f727eef20b3e3918794d984e367b619"),
            cheri=GitRef("my-fdo-fork/cheri-fixes", "6c2bbc30672fb210565cb788b36480898b647398"),
            extra_notes="c11 atomics and provenance fixes"),
    Project("freetype2", project_name="freetype2",
            baseline=GitRef("my-fdo-fork/baseline", "5d27b10f4c6c8e140bd48a001b98037ac0d54118"),
            cheri=GitRef("my-fdo-fork/cheri-fixes", "f7c6a06cb7458c8972955ebd698058d0957a0a47"),
            extra_notes="c11 atomics and provenance fixes"),
    Project("libjpeg-turbo", project_name="libjpeg-turbo",
            baseline=GitRef("mygithub/baseline", "4d9f256b0184bf8ee6e59e8cdf34c7d577d81b27"),
            cheri=GitRef("mygithub/cheri-fixes", "a72816ed07d71e34de07324ede020780d73c5c21"),
            extra_notes="Cast via uintptr_t for alignment"),
    Project("libpng", project_name="libpng",
            baseline=GitRef("origin/libpng16", "a37d4836519517bdce6cb9d956092321eca3e73b"),
            cheri=GitRef("libpng16", "128a7128021d1aab082af720732023d4771fd8ac"),
            extra_notes="Cast via uintptr_t"),
    UnmodifiedProject("icewm", project_name="IceWM",
            baseline=GitRef("icewm-1-4-BRANCH", "0af76ceb261ae1a5a2f863e2a5c5eee1b9de0be2")),
]

unmodified_frameworks = [
    "attica",
    "breeze",
    "breeze-icons",
    "extra-cmake-modules",
    "kactivities",
    "kactivities-stats",
    "karchive",
    "kauth",
    "kbookmarks",
    "kcmutils",
    "kcodecs",
    "kcompletion",
    "kconfig",
    "kconfigwidgets",
    "kcoreaddons",
    "kcrash",
    "kdbusaddons",
    "kdeclarative",
    "kdecoration",
    "kded",
    "kfilemetadata",
    "kframeworkintegration",
    "kglobalaccel",
    "kguiaddons",
    "ki18n",
    "kiconthemes",
    "kidletime",
    "kimageformats",
    "kinit",
    "kio",
    "kio-extras",
    "kirigami",
    "kitemmodels",
    "kitemviews",
    "kjobwidgets",
    "knewstuff",
    "knotifications",
    "knotifyconfig",
    "kpackage",
    "kparts",
    "kpeople",
    # "kpty",
    # "kquickcharts",
    "krunner",
    "kscreenlocker",
    "kservice",
    "ksyndication",
    "ksyntaxhighlighting",
    "ktextwidgets",
    "kunitconversion",
    "kwidgetsaddons",
    "kwindowsystem",
    "kxmlgui",
    "libkscreen",
    "libksysguard",
    "libqrencode",
    "phonon",
    #"plasma-desktop",
    #"plasma-framework",
    #"plasma-workspace",
    "prison",
    "qqc2-desktop-style",
    "solid",
    "sonnet",
    "threadweaver",
]
print("unmodified_frameworks=", len(unmodified_frameworks))
dsbd_projects.append(UnmodifiedDirectories(directories=unmodified_frameworks, project_name="unmodified framworks",
                                           base_directory="kde-frameworks"))
dsbd_projects.append(UnmodifiedDirectories(directories=["libxau", "libxcb", "libxtrans", "libxext", "libxfixes",
                                                        "libxi", "libxrender", "libice", "libsm", "libxmu",
                                                        "build/libxcb-riscv64-purecap-build"],
                                           project_name="unmodified x11 pt1"))
dsbd_projects.append(UnmodifiedDirectories(directories=["libxpm", "libxft", "libxrandr", "libxcomposite", "libxdamage",
                                                        "libxcb-render-util", "xorg-macros", "libxcursor",
                                                        "libxcb-keysyms", "libxcb-wm", "xbitmaps", "xkeyboard-config",
                                                        "xcbproto", "libfontenc", "libxcb-cursor", "libxcb-image",
                                                        "libxcb-util", "libxkbcommon", "libxkbfile", "libxtst",
                                                        "xorg-font-util", "xorg-pthread-stubs"],
                                           project_name="unmodified x11 pt2"))
dsbd_projects.append(UnmodifiedDirectories(directories=["xev", "xeyes", "xprop", "xauth", "xkbcomp", "twm", "xsetroot"],
                                           project_name="X11 programs"))
dsbd_projects.append(UnmodifiedDirectories(directories=["openjpeg", "pixman", "lcms2", "libudev-devd", "mtdev",
                                                        "libevdev", "libintl-lite", "libexpat",
                                                        "libinput", "shared-mime-info", "exiv2", "epoll-shim",
                                                        "qt5/qtx11extras", "qt5/qtquickcontrols2",
                                                        "qt5/qttools", "qt5/qtquickcontrols"],
                                           project_name="unmodified libraries"))
projects = dsbd_non_total_count_projects + dsbd_projects
# endregion

all_cheribuild_targets = [
    "attica", "breeze", "breeze-icons", "dolphin", "epoll-shim", "exiv2", "extra-cmake-modules",
    "fontconfig", "freetype2", "gwenview", "icewm", "kactivities", "kactivities-stats", "karchive", "kauth",
    "kbookmarks", "kcmutils", "kcodecs", "kcompletion", "kconfig", "kconfigwidgets", "kcoreaddons", "kcrash",
    "kdbusaddons", "kdeclarative", "kdecoration", "kded", "kfilemetadata", "kframeworkintegration", "kglobalaccel",
    "kguiaddons", "ki18n", "kiconthemes", "kidletime", "kimageformats", "kinit", "kio", "kio-extras", "kirigami",
    "kitemmodels", "kitemviews", "kjobwidgets", "knewstuff", "knotifications", "knotifyconfig", "kpackage", "kparts",
    "kpeople", "krunner", "kscreenlocker", "kservice", "ksyndication", "ksyntaxhighlighting", "ktextwidgets",
    "kunitconversion", "kwidgetsaddons", "kwin", "kwindowsystem", "kxmlgui", "lcms2", "libevdev", "libexpat",
    "libfontenc", "libice", "libinput", "libintl-lite", "libjpeg-turbo", "libkscreen", "libksysguard", "libpng",
    "libqrencode", "libsm", "libudev-devd", "libx11", "libxau", "libxcb", "libxcb-cursor", "libxcb-image",
    "libxcb-keysyms", "libxcb-render-util", "libxcb-util", "libxcb-wm", "libxcomposite", "libxcursor", "libxdamage",
    "libxext", "libxfixes", "libxfont", "libxft", "libxi", "libxkbcommon", "libxkbfile", "libxmu", "libxpm",
    "libxrandr", "libxrender", "libxt", "libxtrans", "libxtst", "mtdev", "okular", "openjpeg", "phonon", "pixman",
    "plasma-desktop", "plasma-framework", "plasma-workspace", "poppler", "prison", "qqc2-desktop-style", "qtbase",
    "qtdeclarative", "qtgraphicaleffects", "qtquickcontrols", "qtquickcontrols2", "qtsvg", "qttools", "qtx11extras",
    "shared-mime-info", "solid", "sonnet", "sqlite", "systemsettings", "threadweaver", "tigervnc", "twm", "xbitmaps",
    "xcbproto", "xev", "xeyes", "xkbcomp", "xkeyboard-config", "xorg-font-util", "xorg-macros", "xorg-pthread-stubs",
    "xorgproto", "xprop", "xsetroot", "xvnc-server",
    # Fake target for the generated xcb C source code:
    "libxcb-riscv64-purecap-build",
    "xauth",  # needed for ssh forwarding
]

reports = []
missing_projects = set(all_cheribuild_targets)
missing_projects.remove("sqlite")  # ignore sqlite since it was not ported as part of this project
for project in projects:
    reports.append(project.run_cloc())
    if isinstance(project, (Project, UnmodifiedProject)):
        tgt = Path(project.repo_subdir).name
        assert tgt in all_cheribuild_targets, "not in cheribuild targets: " + tgt
        if tgt in missing_projects:
            missing_projects.remove(tgt)
    if isinstance(project, UnmodifiedDirectories):
        for d in project.directories:
            tgt = Path(d).name
            assert tgt in all_cheribuild_targets, "not in cheribuild targets: " + tgt
            if tgt in missing_projects:
                missing_projects.remove(tgt)

if missing_projects:
    print("Did not include:", list(sorted(missing_projects)))
    sys.exit(1)


# No changes were required for SPEC or libxml2, these values are just a simple cloc count without the diff flag
# reports.append(CLOCReport.no_changes_report("SPECINT2006", {"C": 218398, "C++": 25606},
#                                            ClocSummary(blank=47386, comment=49755, code=258461, nFiles=466)))
# reports.append(CLOCReport.no_changes_report("libxml2", {"C": 100},
#                                            ClocSummary(blank=29407, comment=57482, code=231071, nFiles=184)))

@dataclass
class SummaryReport:
    sloc: int = 0
    files: int = 0
    modified_sloc: int = 0
    modified_files: int = 0
    projects: list = field(default_factory=list)

    def combine(self, report: CLOCReport, *, ignore_changes: bool = False) -> None:
        """
        :param report:
        :param ignore_changes: when set only include the totals, but ignore changes
        """
        self.sloc += report.baseline.code
        self.files += report.baseline.nFiles
        if not ignore_changes:
            self.modified_sloc += report.changed_loc_abs
            self.modified_files += report.changed_files_abs
        self.projects.append(report.project)

total = SummaryReport()
subset_totals: defaultdict[str, SummaryReport] = defaultdict(SummaryReport)
for report in reports:
    report.print_info()
    if not report.project.commented:
        total.combine(report)
        if report.project.no_cheri_specific_changes:
            subset_totals["no CHERI"].combine(report)
        subset_totals["CHERI"].combine(report, ignore_changes=report.project.no_cheri_specific_changes)
        if report.main_language.startswith("?"):
            raise ValueError()
        subset_totals[f"{report.main_language}"].combine(report)
        subset_totals[f"CHERI, {report.main_language}"].combine(report, ignore_changes=report.project.no_cheri_specific_changes)

print("------- SUMMARY --------------")
print(f"TOTAL SLOC            {total.sloc:,}")
print(f"SLOC CHANGED          {total.modified_sloc:,}")
print(f"SLOC CHANGED %        {100.0 * (total.modified_sloc / total.sloc):,}")
print(f"FILES CHANGED         {total.modified_files:,}")
print(f"FILES CHANGED %       {100.0 * (total.modified_files / total.files):,}")
print(f"TOTAL FILES           {total.files:,}")
print(f"SLOC / FILE           {total.sloc / total.files:,}")
print("")
for subset_name, summary in subset_totals.items():
    print(f"TOTAL SLOC   ({subset_name})    {summary.sloc:,}")
    print(f"SLOC CHANGED ({subset_name})    {summary.modified_sloc:,}")
    print(f"SLOC CHANGED ({subset_name}) %  {100.0 * (summary.modified_sloc / summary.sloc):,}")
    print(f"TOTAL FILES ({subset_name})     {summary.files:,}")
    print(f"SLOC / FILE ({subset_name})     {summary.sloc / summary.files:,}")
    print(f"FILES CHANGED ({subset_name})   {summary.modified_files:,}")
    print(f"FILES CHANGED ({subset_name}) % {100.0 * (summary.modified_files / summary.files):,}")
    print("")
print("-----------------------------\n")

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
if args.verbose:
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
if args.verbose:
    print(worst_report)
macro_defs += "\n\n\\newcommand*{\\ChangedSlocMax}{" + str(worst_report.changed_loc_abs) + "}\n"
macro_defs += "\\newcommand*{\\ChangedSlocMaxRatio}{" + "{:.2f}\\%".format(worst_report.changed_loc_percent) + "}\n"
macro_defs += "\\newcommand*{\\ChangedSlocMaxProject}{" + str(worst_report.project_name_for_latex) + "}\n"

if args.verbose:
    print(macro_defs)
Path(this_dir / "changes-macros.tex").write_text(macro_defs)
