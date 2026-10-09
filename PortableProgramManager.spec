# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        # Bundle the icon assets so icons.py can load them at runtime.
        # Source path is relative to this spec file.
        # Destination matches Path(__file__).parent / "icons" inside the bundle.
        ('src/portable_manager/ui/icons', 'portable_manager/ui/icons'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ----- PySide6/Qt modules NOT used by this app -----
        # 3-D, multimedia, and specialty modules (biggest savings)
        'PySide6.Qt3DAnimation', 'PySide6.Qt3DCore', 'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DRender',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel',
        'PySide6.QtWebSockets',
        'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D',
        'PySide6.QtQml', 'PySide6.QtQmlModels',
        'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'PySide6.QtGraphs', 'PySide6.QtGraphsWidgets',
        'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
        'PySide6.QtSvg', 'PySide6.QtSvgWidgets',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtSensors',
        'PySide6.QtSerialPort', 'PySide6.QtSerialBus',
        'PySide6.QtPositioning', 'PySide6.QtLocation',
        'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtStateMachine',
        'PySide6.QtSql', 'PySide6.QtXml',
        'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtTest',
        'PySide6.QtUiTools',
        'PySide6.QtTextToSpeech', 'PySide6.QtSpatialAudio',
        'PySide6.QtHttpServer', 'PySide6.QtGrpc', 'PySide6.QtProtobuf',
        # ----- Other unused modules -----
        'unittest', 'doctest', 'pdb', 'tkinter', 'lib2to3',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PortableProgramManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Embedded into the .exe header — shown in Explorer, taskbar, and Alt+Tab.
    icon=['src/portable_manager/ui/icons/PortableProgramManager_icon_optimized.ico'],
)
