"""Publish a completed Vite build; retain old hashed chunks for open browser tabs."""
import argparse,os,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def deploy(source=None,target=None):
    source=Path(source or ROOT/'frontend'/'dist');target=Path(target or ROOT/'monitor'/'static')
    if not (source/'index.html').is_file() or not (source/'assets').is_dir():
        raise SystemExit('Run npm ci and npm run build in frontend first')
    (target/'assets').mkdir(parents=True,exist_ok=True)
    for file in (source/'assets').iterdir():
        if file.is_file():
            temporary=target/'assets'/(file.name+'.tmp')
            shutil.copyfile(file,temporary);os.replace(temporary,target/'assets'/file.name)
    temporary=target/'index.html.tmp';shutil.copyfile(source/'index.html',temporary);os.replace(temporary,target/'index.html')
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source');parser.add_argument('--target');args=parser.parse_args();deploy(args.source,args.target)
    public_source=ROOT/'frontend'/'dist-public'
    if not args.source and not args.target and (public_source/'index.html').is_file():
        deploy(public_source,ROOT/'monitor'/'public_static')
    print('Frontend published; previous hashed assets retained')
