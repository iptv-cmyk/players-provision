Script for provisioning players on IBS
python prov.py -h

Installation on windows:
Open powershell
winget install --id Git.Git -e --source winget
winget install --id Python.Python.3.12 -e --source winget
git clone https://github.com/iptv-cmyk/players-provision.git
python.exe prov.py

Installation on Linux (terminal):
git clone https://github.com/iptv-cmyk/players-provision.git
python prov.py
