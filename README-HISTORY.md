> 历史说明归档：包含旧参数、旧布局和已删除启动脚本，仅供追溯；当前使用方法以 [README](README.md) 为准。

# eeSDR AD9361 视频自发自收

**1080p30 / 64QAM 参数按钮**：在设置页点击“1080p30 原画质 / 64QAM”，或运行 `run_1080p30_64qam.bat`。加载 1920×1080 / 30 fps、原码流、64QAM、3 MSPS、1024 FFT、每批 256 包、TX 0 dB / RX 28 dB、CRC 和实时模式，并选择新下载的 1080p 素材。30 秒真实 RF 窗口测试收到 902/902 帧、0 迟到、7.743 Mbps、哈希一致，详见 [1080p30 64QAM 验证](1080p30-64QAM验证.md)。

**原画质入口：`run_original_realtime.bat`，16QAM、3 MSPS、1024 FFT、持续 DMA 收发。** 新后端用独立的发送、接收、解码线程，缓冲复用；后台准备下一块波形，诊断复用接收 IQ。最新加入临时 IIO 读写错误自动恢复，RX 内核缓冲改为 8 块；开启信道状态页实测 4.484 Mbps、902/902 帧、0 迟到、最大显示间隔 63 ms，媒体哈希一致。此前 4 块内核缓冲的 60 秒测试达到 6.921 Mbps、1800/1800 帧，但后续发现偶发 EIO，已增加恢复处理。起播仍需约 3 秒缓冲。详细证据见 [原画质实时吞吐优化](原画质实时吞吐优化.md)。

## 上位机稳定性修复（2026-10-15）

- 原码流解码异常会取消流水线并向界面报告错误，避免发送线程卡在满队列；预缓冲及播放等待支持及时停止。
- 原码流模式保留完整文件，界面禁用“最多帧数”并设为 0；CLI/API 传入非零帧数限制会明确报错。需限制帧数时请使用 stream/frame 模式。
- 原码流预览按源帧 PTS 调度，保留变帧率素材的时间间隔；缺失时间戳时按标称帧间隔回退。
- 视频预览使用独立的单槽队列，界面繁忙时丢弃过时预览，不影响接收文件；控制消息不再排在视频预览后面。
- 左右预览分别适配窗口大小，拖动分隔条不会因共享较大图像而裁掉原码流画面。

验证：原有 36 项测试和新增 8 项回归测试通过，包含无板 OFDM、Windows spawn 子进程、异常注入、变帧率文件及 Tk 画布检查；此次修复未进行真实射频复测。

运行全部测试：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。

**独立连续 CSI**：双击 `run_csi_live.bat`，或在“信道状态”页开始采集。实测活动采集约 20 Hz，带 10 条子载波时序、幅相热图及 CSV；与视频互斥。详见 [CSI 说明](OFDM-CSI说明.md)。

新增 **720p 原画质透传**：`run_720p_original.bat`，不重编码，接收媒体载荷及逐帧像素与下载原片一致。两段实拍视频完整传输通过，但当前真实链路净吞吐约 1.0–1.3 Mbps，**稳定实时 720p30 未通过**。详见 [原画质验证](720p30原画质验证.md)。

新增 **OFDM 1080p60 低码率试验**：双击 `run_ofdm_1080p60.bat`。1920×1080 / 60 fps / 400 kbps / 16QAM 的 10 秒真实射频测试收到并显示 600/600 帧；画质受到低码率限制。完整数据见 [1080p60 验证](OFDM-1080p60验证.md)。

**2026-10-15：新增 Python OFDM 视频链路。** 双击 `run_ofdm_video.bat`，详见 [OFDM 视频验证说明](OFDM-视频验证.md)。新增 256 FFT / 64 CP、训练均衡及导频相位校正，支持同板双天线视频自发自收；下文历史测试使用旧单载波波形，不能当作 OFDM 测试结果。

连续解码版：窗口标题含“连续解码版”。H.264 解码进程在整个演示期间常驻，各视频段不再反复启动 FFmpeg；AUD 分隔访问单元，保存的像素已用独立解码逐段比对验证。30 秒 720p30 射频验证收到 900/900 帧，0 丢帧、0 迟到、0 预览跳帧，实际绘制 900 次，60 段共用 1 个解码进程，无回退，起播 2.31 秒、播放 29.98 秒。信道记录 126 次真实估计。报告为 `examples/big_buck_bunny_channel_continuous.json`。这些是本次运行的统计，不保证屏幕呈现无任何抖动；原动画为 24 fps，转换 30 fps 仍存在重复帧。

右下预留区现已加入“信道状态 / 感知数据采集”：相对幅度/相位、前导拟合质量、时序与等效频响，并自动记录 CSV 和配置 JSON。说明见 `CHANNEL-SENSING.md`。当前为已知前导的单载波等效信道估计，非 Wi-Fi 逐子载波 CSI，没有实现人体感知算法。

演示布局：左上发送画面，左下并排放置发送与接收星座图；右上接收画面，右下为空白预留区，后续可添加其他功能。

## 播放方式与刷新优化

设置页新增“播放方式”下拉框：**预缓冲**沿用设置的段长度和起播缓冲段数，优先稳定播放；**实时（低延迟）**将有效视频段限制到最多 0.5 秒、起播只缓冲 1 段，增加最多 0.25 秒抖动余量，超过 100 ms 迟到的预览帧会跳过并显示每段最新帧，录制文件仍写入完整接收帧。实时选项减少等待，但不表示摄像头实时输入，也不能保证链路不足时不卡顿。修改在下一次开始时生效。

视频预览按最多 30 fps 推送；发送和接收像素一致时只编码/传输一份预览，并共享显示图像，接收像素不同时仍独立显示，不会用发送画面掩盖接收错误。界面轮询改为扣除绘制时间调度，减少固定等待叠加。

录制移到独立的有界队列/线程，避免写 MP4 阻塞播放调度。批量接收不完整时保留已经正确收到的包，只重发缺失包；64QAM/256QAM 的射频缺失包重传使用 16QAM，视频主数据仍使用设置的调制。独立采样会跟随实际发射调制，星座图标注实际符号阶数。该自适应重传仅用于本程序同板自发自收，不是两个独立端点之间的自适应协议。

星座图使用独立的单槽最新数据队列，避免排在视频事件后。采样与显示目标上限为 20 Hz，传输任务优先，实际速率取决于 USB 与基带处理。界面显示实际刷新 Hz 和回调到界面的队列延迟；该延迟不是天线到屏幕的硬件采样延迟。星座点显示实际捕获后的同步/均衡符号，不再要求 CRC 通过才更新；视频接收仍按 CRC 复选框决定是否拒收。

已实测的优化比较：旧画质版 30 秒只显示约 401 次预览；独立最新队列与共享图像版显示 875 次，900 帧全部收到，仅选择性重传 2 个包，最大迟到约 211 ms，播放 29.98 秒。录制分离及低延迟抖动余量版的 10 秒射频测试收到 300/300 帧，显示 290 次预览，起播 2.47 秒，平均星座队列延迟 14.5 ms；8 次预览因迟到而跳过，保存文件不丢帧。数据为不同时间运行的本地链路结果，信道变化仍会影响重试与卡顿。

最终低延迟模式的 10 秒界面射频验证：300/300 帧，0 丢帧、0 整批重试，起播 2.34 秒，播放 9.98 秒；显示 288 次视频预览、52 次新的星座数据，平均星座队列延迟 15.9 ms。7 次预览因超过迟到门限被跳过，最大播放迟到 156 ms，故不宣称完全无卡顿。此时星座平均约 5 次/秒，20 Hz 是采样/显示上限，不是硬件实际达到的固定频率。

顶部现在使用“演示”和“设置”两个选项卡。演示页显示独立的发送/接收画面、星座图和开始/停止按钮；文件、射频参数、编码选项及 CRC 复选框放在设置页。画面默认等比例铺满预览区域，超出区域的边缘居中裁剪，不拉伸；改变窗口大小或拖动左右分隔条后，即使视频已结束，最后一帧也会重新铺满。预览裁剪不改变保存视频的完整画面。

## 画质与 CRC 开关

工具栏的 **CRC 校验** 是复选框，默认勾选；修改后在下一次“开始发送并接收”时生效。勾选时拒收 CRC 不正确的数据包；取消后跳过接收 CRC 计算与拒收，只保留长度、包头和序号检查，未校验的数据会尝试解码。界面明确显示“CRC 未校验”，不能将其当作完整性验证通过；视频解码失败时仍显示丢失。报文中的 CRC 字段仍保留以兼容原格式。CLI 使用 `--crc` / `--no-crc`。

CRC 开关不改善压缩画质。为减少预览的二次损失，界面预览宽度提高到最多 1280 像素、JPEG 质量由 85 提高到 95；保存和传输分辨率独立于预览。编码档位可选 `veryfast/fast/medium/slow`，更精细的编码增加 CPU 开销。

新画质配置的完整图形界面射频测试：900/900 帧、0 丢帧、0 重试、起播 8.09 秒、播放 30.00 秒；140 帧超过一个帧周期的迟到阈值，最大迟到 121 ms。画质有所改善，但界面仍有短暂调度抖动。数据和视频保存在 `examples/big_buck_bunny_1080p30_quality_gui.json/.mp4`。

`run_1080p_quality.bat` 提供单独的 1080p 画质试播：750 kbps、medium 编码、2 秒视频段、2 段起播缓冲，不修改原有 720p 默认配置。10 秒命令行射频测试 300/300 帧、0 重试，起播 8.27 秒、播放 9.98 秒，最大迟到 60.7 ms。三个抽样帧相对源视频的保存文件 PSNR 由原 500 kbps 的 28.30/25.07/22.75 dB 提高到 30.19/28.54/26.21 dB；这只是本地抽样比较，不能代替完整感知画质评价。更高画质仍受射频净吞吐约 1 Mbps 限制，750 kbps 不等同于高码率高清。

界面现在使用独立的发送端和接收端面板：画面按窗口大小等比例放大，左右面板可拖动分隔条调整，各自下方显示星座图。“参数与文件”按钮可展开设置。接收星座图在批量传输间隙独立读取约三个数据包大小的新 IQ 捕获，目标采样频率为 8 Hz，传输任务优先；实际更新频率取决于 DMA、USB 和解调耗时。没有新的有效采样超过 1 秒时标记为暂无新采样，不生成模拟动画。结束射频发送后最后的预缓冲视频仍可能继续播放。放大界面的预览最多推送 20 次/秒以限制界面开销，传输和保存的视频仍为 30 fps。

## 高清动画演示素材

双击 `run_hd_video.bat` 自动播放并射频传输《Big Buck Bunny》的原生 1280×720 片段，比旧的放大测试图更适合观察画质。源视频为 24 fps，传输时转换到 30 fps，无音频。采用 500 kbps、1 秒视频段、2 段起播缓冲，起播约 4 秒。10 秒命令行射频验证收到 300/300 帧，0 丢帧、0 迟到、0 重试。素材署名、镜像与改编说明见 `examples/BigBuckBunny-SOURCE.md`。

窗口标题包含素材文件名，输出为 `examples/big_buck_bunny_received_gui.mp4`，报告为 `examples/big_buck_bunny_received_gui.json`。当前码率仍为 500 kbps，复杂纹理仍会被压缩；不能把原始素材的清晰度当作接收端无损画质。

## 当前板卡射频验证：720p30 / 500 kbps（2026-10-08）

已在本次连接的板卡上完成 **30 秒真实射频自发自收**：1280×720、30 fps，900/900 帧，60/60 视频段、1870 包、0 整批重试、0 丢帧、0 迟到帧；起播等待 2.33 秒，播放用时 29.98 秒。板卡被 IIO 识别为 PlutoSDR，测试 URI 为 `usb:1.5.5`（重新连接后可能变化）。这是当前环境下的一次持续测试，尚不能推广为任意距离和信道下的稳定性保证。

**使用方式：双击 `run_720p30.bat` 或 `run_720p30_rf.bat`。** 窗口已支持“720p30 射频”预设，默认选择本次生成的原生 720p 运动纹理测试视频。选好输入文件后开始。CLI 可运行：

```powershell
.\.venv\Scripts\python.exe -m video_loopback.cli examples/test_30s_720p30_detail.mp4 --profile 720p30-rf --uri usb:1.5.5 --mode rf --output examples/received_720p30_rf_30s_500k.mp4 --report examples/received_720p30_rf_30s_500k.json
```

已验证预设：**64QAM、16 MSPS、每符号 4 个采样、每批最多 64 包、TX 0 dB、RX 10 dB、H.264 500 kbps、0.5 秒视频段、起播预缓冲 2 段**。增加缓冲解决了视频段到达时间波动引起的播放迟到；不会提高射频吞吐，也不是零延迟。

| 测试 | 正确收到的帧数 | 播放用时 | 最大迟到 | 结论 |
| --- | --- | --- | --- | --- |
| 500 kbps、10 秒、2 段缓冲 | 300/300 | 9.99 秒 | 0 ms | 通过 |
| 750 kbps、10 秒、2 段缓冲 | 300/300 | 9.98 秒 | 0 ms | 短测通过 |
| 750 kbps、30 秒、2 段缓冲 | 900/900 | 32.48 秒 | 4187 ms | 4 次整批重试，出现积压，不能作为稳定实时配置 |
| 500 kbps、30 秒、2 段缓冲 | 900/900 | 29.98 秒 | 0 ms | 本次持续测试通过 |

随机数据射频测速净吞吐约 **1.27 Mbps**（15 批、64 包/批）；500 kbps 的 30 秒视频任务中净传输约 **0.97 Mbps**。因此当前实现支持低码率 720p30，不能据此宣称高画质 3 Mbps 的 720p30 已实现。500 kbps 会牺牲运动细节与纹理画质。

输出：`examples/received_720p30_rf_30s_500k.mp4`；统计：`examples/received_720p30_rf_30s_500k.json`。失败对照和各档测速报告也保留在 `examples/`。本次增加了起播缓冲及真实射频预设，未修改 FPGA 或固件，也未引入 OFDM。

## 上位机改造及早期仿真记录（2026-10-08）

早期 `720p30` CLI 预设用于 3 Mbps 高吞吐仿真；当前实际板卡请使用上方的 `720p30-rf` 预设。旧的低速参数仍可通过“低速预设”恢复。下面的 3 Mbps 参数未在真实板卡上达到实时传输。

预设：1280×720、30 fps、H.264 3000 kbps、16QAM、8 MSPS、每符号 2 个采样、每批 16 包、0.2 秒独立视频段。CLI 中显式传入的参数覆盖预设。输入视频的帧率可由 FFmpeg 转换到目标帧率。

改造内容：

- 一次 DMA 更新发送一批连续排列的数据包；一次匹配滤波解出多个 CRC 正确的包，自动排除重复包与旧视频段的包。
- 删除原先每包固定等待 30 ms 的路径；可用 CLI `--settle-ms 30` 加回每批等待，辅助硬件排查。
- 支持每符号 2/4/8 个采样，射频滤波带宽随符号率调整；CPU 使用单精度 DSP 和 SciPy FFT。
- FFmpeg 按输入时间边编码边输出已完成的短视频段；无需预编码整个文件。发送、解码和播放使用流水线，接收解码队列有界。
- H.264 码率缓冲按视频段长度设置，减小开头的视频数据突发。GUI 只发送缩小预览到界面进程，保存的视频仍为完整分辨率。
- 新增随机有效载荷测速和 JSON 统计。输出视频帧率为 30 fps 本身不能证明实时传输，应同时看吞吐、播放耗时、迟到帧和最大延迟。

**链路边界：**仍然是同一块板卡 TX/RX 自发自收。每批在循环 DMA 中重复，直到本机接收成功，再换下一批；批内连续，批间存在主机处理与 DMA 切换间隔。这不是无间隙持续 DMA，也不是两个独立端点之间的 ACK 协议。目前提供 CRC 检测和循环捕获/整批重试，尚未实现前向纠错、独立收发板卡协议或摄像头输入。不同射频本振之间的频偏跟踪也不在此次验证范围内。

### 先测板卡吞吐

在本目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m video_loopback.benchmark --profile 720p30 --mode digital --batches 100 --report examples/benchmark_digital.json
.\.venv\Scripts\python.exe -m video_loopback.benchmark --profile 720p30 --mode rf --tx-gain 0 --rx-gain 10 --batches 100 --report examples/benchmark_rf.json
```

查看 `net_mbps`（正确收到的有效载荷净速率）、`failed_batches` 和 `batch_p95_ms`。对于 3 Mbps 视频，先以持续净吞吐 ≥5 Mbps 且无失败批次作为试播门槛，再用长视频评估突发、迟到和无线稳定性。`suggested_video_kbps` 按测速净吞吐的 60% 给出初始码率，存在失败批次时为 0。理论调制速率 16 Mbps 不能代替 USB、DMA 和软件处理后的净速率。

如果不足 5 Mbps，可分别测试 `--batch-packets 8/16/32`、`--sps 2/4`、`--modulation 16qam/64qam`（每次传一个值），并降低视频码率。提高采样率会增加 USB IQ 流量；提高 QAM 阶数会增加对信噪比的要求，都需要实测。

### 视频运行

```powershell
# 随机纹理运动视频比原来放大的小画面更适合测试压缩负载
.\.venv\Scripts\python.exe -m video_loopback.test_video --output examples/test_3s_720p30_detail.mp4 --seconds 3 --fps 30 --width 1280 --height 720 --detail
.\.venv\Scripts\python.exe -m video_loopback.cli examples/test_3s_720p30_detail.mp4 --profile 720p30 --mode simulate --output examples/received_720p30_simulate.mp4 --report examples/received_720p30_simulate.json

# 板卡内部数字回环
.\.venv\Scripts\python.exe -m video_loopback.cli examples/test_3s_720p30_detail.mp4 --profile 720p30 --mode digital --output examples/received_720p30_digital.mp4 --report examples/received_720p30_digital.json

# 射频回环：增益沿用旧测试起点，须按当前接收幅度调整
.\.venv\Scripts\python.exe -m video_loopback.cli examples/test_3s_720p30_detail.mp4 --profile 720p30 --mode rf --tx-gain 0 --rx-gain 10 --output examples/received_720p30_rf.mp4 --report examples/received_720p30_rf.json
```

2026-10-08 本机仿真验证：30 批随机数据，16 包/批，共 491,520 字节，0 失败，净吞吐约 5.89 Mbps；3 秒运动细节视频，90/90 帧，15 段、1111 包、0 重试，接收码率约 3.01 Mbps，起播约 0.62 秒、播放 2.98 秒、最大迟到 50 ms。视频任务中的净传输速率约 4.82 Mbps，包含编码/解码并行运行时的 CPU 竞争。仿真信道使用噪声、固定相位和采样偏移，不覆盖真实频偏、采样时钟漂移和多径。

报告和接收视频保存在 `examples/benchmark_720p30_simulate.json`、`examples/received_720p30_simulate.json`、`examples/received_720p30_simulate.mp4`。早期改造时 `iio_info -s` 未发现已连接的 IIO 设备，所以上述早期结果均为电脑仿真数据；后续连接板卡后的实测结果见本文顶部。

Windows 应用通过同一块 AD9361 板卡的 TX1/RX1 双天线发送、接收视频。界面并排显示发送帧、CRC 校验通过的接收帧，以及实时发送与接收星座图。板卡通过 libiio 的 `usb:` URI 连接。

## 启动

在本目录双击 `run.bat`，或在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe -m video_loopback.gui
```

首次启动由 `run.bat` 创建虚拟环境并安装 `requirements.txt`。运行前关闭 IIO Oscilloscope，避免同时占用板卡 DMA。窗口默认加载 `examples/test_30s_30fps_96x54.mp4`，采用 RF、64QAM、H.264 流传输、30 fps、96×54、15 kbit/s、TX 增益 0 dB、RX 增益 10 dB。这组参数已在当前双天线板卡上验证；其他环境下需按接收幅度调整 RX 增益。

`stream` 模式将视频编码成独立的 1 秒 H.264 段，经分包、QAM 调制、射频发送、CRC 校验后播放并保存为 MP4。输出视频按设置的帧率播放；接收端需要先收到并解码第一个视频段，因此有起播延迟。低分辨率配置消除了长时间播放时的累积延迟，但不是物理意义的零延迟。FFmpeg 子进程在 Windows 上以无控制台窗口方式启动。当前没有音频。`frame` 模式逐帧发送 JPEG，便于单帧调试，但不适合实时播放。

- `simulate`：不连接板卡的电脑内仿真。
- `digital`：AD9361 芯片内部数字回环。
- `rf`：TX1/RX1 或 TX2/RX2 射频收发。通道 0 对应 TX1/RX1，通道 1 对应 TX2/RX2。

多台 USB IIO 设备时，用 `Windows-VS-2022-x64\iio_info.exe -s` 查询并填写具体的 `usb:X.Y.Z` URI。RF 默认中心频率 2400 MHz、IQ 采样率 2 MSPS、符号率 250 ksym/s。

## 实测结果

同一块 PlutoSDR Rev.C、TX1/RX1 双天线、2.4 GHz、30 秒测试视频：

| 调制 | TX / RX 增益 | 测试 | 结果 |
| --- | --- | --- | --- |
| 64QAM | 0 / 10 dB | 30 fps、30 秒 | 900/900 帧、123 包、0 重传、0 迟到帧；命令行总耗时 32.61 秒 |
| 64QAM | 0 / 10 dB | 96×54、15 kbps、30 fps、30 秒，GUI | 900/900 帧、64 包、0 重传；播放 29.97 秒，8 帧短暂迟到，最大 125 毫秒；起播等待 3.83 秒 |
| 64QAM | 0 / 10 dB | 60 fps、30 秒 | 1800/1800 帧、124 包、0 重传、0 迟到帧；命令行总耗时 32.77 秒 |
| 256QAM | 0 / 71 dB（双最大） | 5 秒视频 | 0/300 帧；采样约 68%～82% 接近 ADC 上限，严重削顶 |
| 256QAM | 0 / 10 dB | 30 fps、30 秒 | 690/900 帧、7 个视频段失败、23 次重传 |
| 256QAM | 0 / 71 dB（双最大） | 30 fps、5 秒 | 0/150 帧；全部视频段失败 |

板卡在 2.4 GHz 下报告 RX 手动增益范围为 -3～71 dB，TX 最大为 0 dB。双最大增益无法用于这里的 256QAM 视频：RX 过载使星座点合并，CRC 无法通过。降低 RX 增益消除了削顶，但当前双天线链路上的 256QAM 仍有明显误包。稳定演示请使用已验证的 64QAM、TX 0 dB、RX 10 dB。

160×90、30 kbps 的窗口测试虽然收到 900/900 帧，但用时 40.62 秒且发生明显累积延迟。将分辨率和码率降到 96×54、15 kbps 后，窗口播放 30 秒视频只用 29.97 秒；画质降低，起播仍需等待首个视频段。

低延迟 30 fps 接收视频：`examples/stream_rf_64qam_96x54_15k_gui.mp4`，统计见 `examples/stream_rf_64qam_96x54_15k_gui.json`。30 fps 256QAM 对照视频：`examples/stream_rf_256qam_tx0_rx10_30fps_30s.mp4`；黑帧表示对应视频段未通过校验。

## 命令行复现

```powershell
.\.venv\Scripts\python.exe -m video_loopback.cli .\examples\test_30s_30fps_96x54.mp4 --output .\examples\received.mp4 --mode rf --modulation 64qam --transport stream --bitrate-kbps 15 --width 96 --height 54 --uri usb: --channel 0 --tx-gain 0 --rx-gain 10 --fps 30 --max-frames 900
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

重新生成测试视频：

```powershell
.\.venv\Scripts\python.exe -m video_loopback.test_video --output .\examples\test_30s_30fps_96x54.mp4 --seconds 30 --fps 30 --width 96 --height 54
```

## 参考

- [ADI pyadi-iio AD9361 接口](https://analogdevicesinc.github.io/pyadi-iio/devices/adi.ad936x.html)
- [ADI AD9361 数据手册](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9361.pdf)
- [FFmpeg 文档](https://ffmpeg.org/ffmpeg.html)
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)
- [Pluto-DATV-test](https://github.com/LamaBleu/Pluto-DATV-test)

`Windows-VS-2022-x64/` 中的 libiio 0.25 DLL 来自 [ADI 官方 v0.25 Windows 包](https://github.com/analogdevicesinc/libiio/releases/tag/v0.25)；许可证见 [LICENSE-libiio.txt](LICENSE-libiio.txt)。
