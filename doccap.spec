# PyInstaller 打包配置：生成「AutoDocCap.app」。用 ./build.sh 调用，不要直接运行。
import os

VERSION = os.environ.get("DOCCAP_VERSION", "0.1.0")

a = Analysis(
    ["app.py"],
    datas=[("ui", "ui")],
    # 这些模块在函数内部按需导入，显式列出以防漏打
    hiddenimports=["Quartz", "Vision", "AppKit", "Foundation", "objc", "PyObjCTools.AppHelper",
                   "pymupdf", "webview.platforms.cocoa"],
    excludes=["tkinter", "docx", "PyInstaller"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="doccap", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="doccap")
app = BUNDLE(
    coll,
    name="AutoDocCap.app",
    icon="assets/AutoDocCap.icns",
    bundle_identifier="io.github.doccap",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "AutoDocCap",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
