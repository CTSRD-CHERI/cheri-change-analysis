#!/bin/sh
cd ~/cheri/build/spec2006-128-build/spec/benchspec/CPU2006
cloc '--include-lang=C,C++,C/C++ Header,Assembly' '--exclude-content=\bDO NOT EDIT\b' --verbose=1 --file-encoding=UTF-8 --processes=8 401.bzip 445.gobmk 456.hmmer 458.sjeng 462.libquantum 464.h264ref 471.omnetpp 473.astar 483.xalanbmk