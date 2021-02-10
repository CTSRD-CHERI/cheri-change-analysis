import pandas as pd
from pathlib import Path
import json

fname = "FreeBSD kernel (full-by-file).report.json.diff.e10b4e4c363cb013ee6c318100cc04d2bd620588.83c181c7a1985d84312f79a5506dcc0063aeeb76.json"
with (Path(__file__).parent / "reports" / fname).open("r") as f:
    j = json.load(f)

sum = j["SUM"]
del j["SUM"]
del j["header"]


df_add = pd.DataFrame.from_dict(j["added"], orient="index").sort_values('code', ascending=False)
df_same = pd.DataFrame.from_dict(j["same"], orient="index").sort_values('code', ascending=False)
df_removed = pd.DataFrame.from_dict(j["removed"], orient="index").sort_values('code', ascending=False)
df_modified = pd.DataFrame.from_dict(j["modified"], orient="index").sort_values('code', ascending=False)

print("======= ADDED ========")
print(df_add.head(15).to_string())
print("======= REMOVED ========")
print(df_removed.head(15).to_string())
print("======= MODIFIED ========")
print(df_modified.head(15).to_string())
