# Build with: python -m PyInstaller --noconfirm LuxLink-SDR.spec
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_submodules

root=Path(SPECPATH)
runtime=root/'Windows-VS-2022-x64'
binaries=[(str(path),'Windows-VS-2022-x64') for path in runtime.glob('*.dll')
          if path.name!='libiio-sharp.dll']
datas=[(str(root/'video_loopback'/'assets'),'video_loopback/assets'),
       (str(root/'LICENSE-libiio.txt'),'.')]
a=Analysis([str(root/'luxlink_app.py')],pathex=[str(root)],binaries=binaries,
           datas=datas,hiddenimports=collect_submodules('adi'),
           hookspath=[],runtime_hooks=[],excludes=['pytest','matplotlib','IPython'])
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],name='LuxLink-SDR',debug=False,exclude_binaries=True,
        bootloader_ignore_signals=False,strip=False,upx=False,
        console=os.environ.get('LUXLINK_BUILD_CONSOLE')=='1',
        icon=str(root/'video_loopback'/'assets'/'app-icon.ico'))
collect=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='LuxLink-SDR')
