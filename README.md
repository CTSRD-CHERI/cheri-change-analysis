# CHERI change analysis

This repository contains some scripts that I used to analyze the required CHERI changes for my [PhD dissertation](https://www.cl.cam.ac.uk/techreports/UCAM-CL-TR-949.html), Table 6.1.

The scripts use the [cloc](https://github.com/AlDanial/cloc) git diff support to compare a baseline commit of a git repository against the version with the CHERI changes.
The Git repositories, revisions, and cloc flags can be defined in the `projects` array inside
`compute_loc_changes.py`.
The script will generate a set of JSON reports, a LaTex table with the changes and another LaTeX file with
macros containing the change percentages.
The script currently expects all git repositories that are being analyzed to be checked out under `~/cheri`
