#!/bin/bash
cd /Users/fu2re/Workspace/deluge-copysubtitles/copysubtitles
mkdir temp
export PYTHONPATH=./temp
/Users/fu2re/Workspace/deluge-copysubtitles/venv/bin/python setup.py build develop --install-dir ./temp
cp ./temp/copysubtitles.egg-link /Users/fu2re/.config/deluge//plugins
rm -fr ./temp
