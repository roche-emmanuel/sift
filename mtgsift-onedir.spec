# -*- mode: python -*-

# During spec file development run like so:
#   export PYTHONHASHSEED=1
#   pyinstaller --clean --noconfirm --debug all mtgsift-onedir.spec
# To generate a self-contained executable:
#   pyinstaller --clean --noconfirm             mtgsift-onedir.spec

import sys
sys.setrecursionlimit(5000)

from PyInstaller.compat import is_win, is_darwin, is_linux
from PyInstaller.utils.hooks import collect_submodules

import dask
import distributed
import pyspectral
import satpy
import vispy.glsl
import vispy.io
import xarray
import zarr

block_cipher = None
exe_name = "mtgsift"
main_script_pathname = os.path.join("uwsift", "__main__.py")
_script_base = os.path.dirname(os.path.realpath(sys.argv[0]))

data_files = [
    (os.path.join(os.path.dirname(pyspectral.__file__), "etc"), os.path.join("pyspectral", "etc")),
    (os.path.join(os.path.dirname(dask.__file__), "dask.yaml"), os.path.join("dask")),
    (os.path.join(os.path.dirname(distributed.__file__)),       "distributed"),
    (os.path.join(os.path.dirname(satpy.__file__), "etc"),      os.path.join("satpy", "etc")),
    (os.path.dirname(vispy.glsl.__file__),                      os.path.join("vispy", "glsl")),
    (os.path.join(os.path.dirname(vispy.io.__file__), "_data"), os.path.join("vispy", "io", "_data")),
    (os.path.join(os.path.dirname(xarray.__file__), "static"),  os.path.join("xarray", "static"))
]

for shape_dir in ["ne_50m_admin_0_countries",
                  "ne_110m_admin_0_countries",
                  "ne_50m_admin_1_states_provinces_lakes",
                  "fonts",
                  "colormaps",
                  "grib_definitions"]:
    data_files.append((os.path.join("uwsift", "data", shape_dir), os.path.join("sift_data", shape_dir)))

# Append qml files and icons used therein to data_files
for qml_file in ["timeline.qml", "TimelineRuler.qml"]:
    data_files.append((os.path.join("uwsift", "ui", qml_file), os.path.join("uwsift","ui")))

icon_dir = os.path.join("uwsift", "data", "icons")
data_files.append((os.path.join(icon_dir, "menu.svg"), icon_dir))

# For Cython support (see
# https://pyinstaller.readthedocs.io/en/stable/feature-notes.html#cython-support):
hidden_imports = [
    "ncepgrib2",  # For PyGrib
    "pkg_resources",
    "pyproj",
    "satpy",
    "shapely",
    "skimage",
    "skimage.measure",
    "sqlalchemy",
    "sqlalchemy.ext.baked",
    "vispy.app.backends._pyqt5",
    "vispy.ext._bundled.six",
    "xarray",
]
hidden_imports += collect_submodules("pkg_resources")
hidden_imports += collect_submodules("pyproj")
hidden_imports += collect_submodules("rasterio")
hidden_imports += collect_submodules("satpy")
hidden_imports += collect_submodules("sqlalchemy")
hidden_imports += collect_submodules("numcodecs")
hidden_imports += collect_submodules("shapely")
hidden_imports += collect_submodules("pyqtgraph")
if is_win:
    hidden_imports += collect_submodules("encodings")
    hidden_imports += collect_submodules("PyQt5")


def _include_if_exists(binaries, lib_dir, lib_pattern):
    from glob import glob
    results = glob(os.path.join(lib_dir, lib_pattern))
    print(lib_dir, lib_pattern, results)
    if results:
        for result in results:
            binaries.append((result, '.'))


# Add missing shared libraries
binaries = []
if not is_win:
    bin_dir   = os.path.join(sys.exec_prefix, "bin")
    lib_dir   = os.path.join(sys.exec_prefix, "lib")
    share_dir = os.path.join(sys.exec_prefix, "share")
    # Add ffmpeg
    binaries += [(os.path.join(bin_dir, 'ffmpeg'), '.')]
    if is_linux:
        binaries += [(os.path.join(lib_dir, 'libfontconfig*.so'), '.')]
else:
    bin_dir   = os.path.join(sys.exec_prefix, "Library", "bin")
    lib_dir   = os.path.join(sys.exec_prefix, "Library", "lib")
    share_dir = os.path.join(sys.exec_prefix, "Library", "share")
    # Add ffmpeg
    binaries += [(os.path.join(bin_dir, 'ffmpeg.exe'), '.')]

#-------------------------------------------------------------------------------
# Add extra pygrib .def files
data_files.append((os.path.join(share_dir, 'eccodes'), os.path.join('share', 'eccodes')))


#-------------------------------------------------------------------------------
# Add ffmpeg dependencies that pyinstaller doesn't automatically find
if is_linux:
    so_ext = '.so*'
elif is_win:
    so_ext = '.lib'
else:
    so_ext = '.dylib'
for dep_so in ['libavdevice*', 'libavfilter*', 'libavformat*', 'libavcodec*', 'libavresample*', 'libpostproc*',
               'libswresample*', 'libswscale*', 'libavutil*', 'libfreetype*', 'libbz2*', 'libgnutls*', 'libx264*',
               'libopenh264*', 'libpng*', 'libnettle*', 'libhogweed*', 'libgmp*', 'libintl*']:
    dep_so = dep_so + so_ext
    if is_win:
        # windows probably doesn't include "lib" prefix on the files
        # and sometimes the actual library files are in bin not lib
        _include_if_exists(binaries, lib_dir.replace('lib', '*'), dep_so[3:].replace(so_ext, '.*'))
    else:
        _include_if_exists(binaries, lib_dir, dep_so)

#-------------------------------------------------------------------------------
# Add pyproy dependencies that pyinstaller doesn't automatically find
#
# TODO: This should go as patch into PyInstaller/hooks/hook-pyproj.py

import pyproj.datadir
data_files.append((pyproj.datadir.get_data_dir(),  os.path.join("share", "proj")))

#-------------------------------------------------------------------------------

a = Analysis([main_script_pathname],
             pathex=[_script_base],
             binaries=binaries,
             datas=data_files,
             hiddenimports=hidden_imports,
             hookspath=[],
             runtime_hooks=[],
             excludes=["tkinter"],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher)
pyz = PYZ(a.pure,
          a.zipped_data,
          cipher=block_cipher)
# FIXME: Remove the console when all diagnostics are properly shown in the GUI

# See
# https://pyinstaller.readthedocs.io/en/stable/spec-files.html#giving-run-time-python-options
# options = [ ('v', None, 'OPTION') ]
options=[]

exe = EXE(pyz,
          a.scripts,
          options,
          exclude_binaries=True,
          name=exe_name,
          debug=False,
          strip=False,
          upx=True,
          console=True )

coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=None,
               upx=True,
               name=exe_name)

if is_darwin:
    app = BUNDLE(coll,
                 name=exe_name + '.app',
                 icon=None,
                 bundle_identifier=None,
                 info_plist={
                     'LSBackgroundOnly': 'false',
                     'NSHighResolutionCapable': 'True',
                 })
