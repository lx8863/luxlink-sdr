# 素材与实验输出

视频、日志、CSI CSV 和逐次运行报告保留在本地，不随 Git 提交。小型脱敏实测摘录见项目根目录的 `docs/reports/`。

## 准备视频

可以在上位机设置页选择自己的 MP4 文件。默认路径 `examples/1080p/ocean_1080p30_original.mp4` 不随仓库提供，首次使用需准备素材或改选其他输入文件。

原默认海洋素材来自 [Sample.Cat](https://sample.cat/en/mp4)，作者 Vincenzo Malagoli，来源文件 `1416529-hd_1920_1080_30fps.mp4`。使用和再分发时请确认来源授权。

也可在项目根目录执行以下命令，下载两段较长海浪素材：

```powershell
.\.venv\Scripts\python.exe examples/download_ocean_materials.py
```

脚本使用系统 `curl.exe`，下载 1080p 分发版本后无重编码封装为 MP4，并完整解码验证。素材分别来自：

- [Ocean Waves Timelapse](https://dareful.com/ocean-waves-timelapse/)，约 31 秒。
- [Ocean Sea Waves](https://dareful.com/ocean-sea-waves-water/)，约 44 秒。

来源标示为 CC BY 4.0，署名 `Video courtesy of Dareful`。来源、许可证和实际媒体参数随下载结果写入 `*.source.json`；请保留这些记录。第三方下载地址可能变化。下载后在设置页选择对应的新文件；该脚本不会自动切换默认输入。

其他探测和界面验证脚本属于开发辅助工具，不是运行上位机的前置步骤。
