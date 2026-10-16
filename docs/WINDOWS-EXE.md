# Windows EXE

从 [最新 Release](https://github.com/lx8863/luxlink-sdr/releases/latest) 下载 Windows x64 便携包，解压后双击 `LuxLink-SDR.exe`。该版本自带 Python、FFmpeg、libiio 和运行依赖，无需安装 Python。视频素材不随程序提供，首次启动后在设置页选择自己的 MP4。

设备驱动仍需正确安装；没有射频板时可在设置页选择 `simulate` 模式。界面仍为中文。程序未做代码签名。

EXE 的默认工作目录为程序旁的 `data/`；该位置不可写时回退到 `%LOCALAPPDATA%\LuxLinkSDR`，接收输出默认位于其 `examples` 子目录；实际输出路径可在设置页修改。启动异常记录在该目录的 `application.log`。这是目录便携版，请保留 `_internal/` 运行库目录，不要只移动 EXE；启动时不需要单文件程序的临时解包。

## 从源码构建

在 Windows x64、Python 3.11 环境中，从项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm LuxLink-SDR.spec
```

输出目录为 `dist/LuxLink-SDR/`，入口为其中的 `LuxLink-SDR.exe`。构建需要仓库自带的 `Windows-VS-2022-x64` DLL 目录和图标资源。`build/`、`dist/` 不纳入 Git。

## 验证实际 EXE

```powershell
.\dist\LuxLink-SDR\LuxLink-SDR.exe --self-test "C:\test-results\luxlink-exe-check"
```

选择一个尚未使用的测试目录。该检查加载 libiio 和 pyadi-iio、调用内置 FFmpeg 生成 12 帧视频、启动实际 Tk 界面和独立接收工作进程，在 `simulate` 模式完成原码流收发，再检查文件完整性及媒体载荷一致性。结果写入 `self-test-result.json`。

仿真验证不替代真实射频验证；后者需要连接正确驱动和固件的设备。

## English

Download the Windows x64 portable bundle from the latest Release, extract it, and run `LuxLink-SDR.exe`. Python, FFmpeg, libiio and runtime dependencies are bundled. Python installation is not required. Supply your own MP4 and install the appropriate radio driver. The UI is Chinese and the executable is unsigned.

Writable data and logs are stored in `data/` next to the EXE, falling back to `%LOCALAPPDATA%\LuxLinkSDR` if necessary. Keep the bundled `_internal/` directory with the EXE; output paths can be changed in Settings. Build with Python 3.11 and `requirements-build.txt`, then run PyInstaller using `LuxLink-SDR.spec`. The `--self-test` option validates the frozen GUI and spawned worker with a 12-frame simulated transfer; it does not validate physical RF behavior.
