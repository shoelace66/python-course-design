# MusicScope 音乐特征分析器

MusicScope 是一个面向 Python 课程设计的本地音乐分析 Web 应用。它只使用 Python 标准库完成 PCM WAV 解码、音频特征提取、规则分类、SQLite 持久化和 HTTP 服务；前端使用原生 HTML、CSS、JavaScript 与 Canvas 绘图，不依赖云服务或第三方 Python 包。

系统能够提取节奏、调性、主导音高、响度、动态范围和频谱等信息，显示四类交互式可视化，并将指标和可视化序列保存到 SQLite。数据库内容可导出为 JSON、CSV 或 SQLite 备份，也可从本系统导出的 JSON/CSV 文件重新导入。

## 1. 运行环境

- Python 3.10 或更高版本
- Windows 10/11，或其他能够运行 Python 和现代浏览器的系统
- Chrome、Edge、Firefox 等现代浏览器
- 不需要安装 NumPy、librosa、Flask 等第三方依赖
- 不需要联网

`requirements.txt` 仅用于说明项目为零第三方依赖，无需执行 `pip install`。

## 2. 快速启动

### Windows 一键启动

双击项目根目录中的：

```text
一键启动.bat
```

脚本会检查 Python 是否存在以及版本是否达到 3.10，然后运行 `app.py`。浏览器通常会自动打开：

```text
http://127.0.0.1:8765
```

如果 8765 端口已被占用，程序会自动选择空闲端口，请以终端中显示的地址为准。关闭启动窗口或在终端按 `Ctrl+C` 可停止服务。

### PowerShell 启动

```powershell
.\启动服务.ps1
```

### 命令行启动

```powershell
python app.py
```

可选参数：

```powershell
python app.py --host 127.0.0.1 --port 8765
python app.py --port 0
python app.py --no-browser
```

- `--host`：监听地址，默认仅本机可访问。
- `--port`：监听端口，设为 `0` 时由系统自动分配。
- `--no-browser`：启动后不自动打开浏览器。

## 3. 使用流程

1. 启动程序并进入“音乐分析”页面。
2. 点击或拖入 PCM WAV 文件，也可点击“生成演示音频”。
3. 点击“开始提取音乐特征”，等待本地分析完成。
4. 查看 BPM、调性、主导音高、dBFS、分类和四类图表。
5. 在“分析记录”中搜索、筛选、恢复或删除历史结果。
6. 在“数据管理”中导出 JSON/CSV、备份数据库，或导入已有数据文件。

没有测试素材时，推荐使用内置演示功能。系统会生成一段约 12 秒、C 大调、120 BPM 的单声道 PCM WAV，再自动完成分析和入库。

## 4. 已实现功能

### 4.1 音频分析

- 解码 8/16/24/32 位 little-endian PCM WAV
- 支持 1～8 声道并自动混合为单声道分析信号
- 使用箱式低通降采样将内部分析采样率限制到不高于 11025 Hz，减少高频混叠
- 以 1024 个采样为一帧、512 个采样为帧移进行短时分析
- 短时能量差分与自相关估计 BPM
- 归一化自相关估计 55～1000 Hz 范围内的主导基频
- 十二平均律换算音名，例如 440 Hz 对应 A4
- Hann 窗与纯 Python Cooley-Tukey FFT 计算平均频谱
- Chroma 十二音级聚合与 Krumhansl-Schmuckler 大小调模板匹配
- 计算 RMS dBFS、峰值 dBFS、动态范围、频谱重心、85% 频谱滚降点和过零率
- 基于 BPM、响度、频谱重心和动态范围输出可解释听感标签

当前规则分类标签包括：

- 静音或近静音
- 激情动感
- 安静舒缓
- 明亮轻快
- 温暖沉稳
- 均衡流行

分类结果是便于课程演示的规则匹配结果，不是经过大规模标注数据训练得到的专业曲风概率。

### 4.2 可视化与播放

- 音频波形图
- 归一化 RMS 音量能量曲线
- 64 个对数频带组成的平均频谱图
- 十二音级 Chroma 能量图
- 本地上传或内置生成的音频可在结果页试听
- JSON 导入的完整序列可以恢复图表；CSV 摘要不包含图表序列

### 4.3 数据库与历史记录

- SQLite 自动建库并启用外键
- 音轨、分析、特征和分类在同一事务中写入
- 指标以及波形、能量、起音、频谱、Chroma、基频轨迹等序列写入数据库
- SHA-256 文件指纹检测重复音频
- 概览页统计总记录数、总时长、平均 BPM、平均响度和分类分布
- 历史记录支持文件名搜索、分类筛选、查看详情和删除
- 删除记录时级联删除关联表数据，并清理对应的本地音频文件

### 4.4 导入导出

- JSON：包含完整指标、分类说明和 `feature_data` 可视化序列，可完整导回系统
- CSV：带 UTF-8 BOM 的扁平摘要，便于 Excel 打开；不包含 `feature_data`
- SQLite：通过在线备份接口下载一致的 `.db` 副本
- JSON/CSV 导入：逐条校验，单条失败不影响其他有效记录
- 重复导入：同一文件哈希且分析时间相同的记录自动跳过
- 数据操作写入 `data_operations` 日志表

## 5. 页面说明

| 页面 | 功能 |
| --- | --- |
| 数据概览 | 统计卡片、听感倾向分布、最近分析、演示入口 |
| 音乐分析 | WAV 选择/拖放、分析进度、指标、播放器、四类图表 |
| 分析记录 | 文件名搜索、分类筛选、详情恢复、记录删除 |
| 数据管理 | JSON/CSV 导出、SQLite 备份、JSON/CSV 导入 |
| 原理与帮助 | 节奏、音高、响度、频谱、分类和数据库原理说明 |

## 6. 项目目录

```text
python课设/
├─ app.py                    # 程序入口
├─ requirements.txt          # 零第三方依赖说明
├─ 一键启动.bat              # Windows 一键启动
├─ 启动服务.ps1              # PowerShell 启动脚本
├─ 一键运行测试.bat          # 自动执行 7 项测试
├─ music_analyzer/
│  ├─ __init__.py
│  ├─ audio.py               # WAV 解码、FFT、特征提取、分类、演示音频
│  ├─ database.py            # SQLite 模型、查询、导入导出与备份
│  └─ server.py              # 本地 HTTP 服务、REST API、静态文件服务
├─ static/
│  ├─ index.html             # 五个业务页面
│  ├─ style.css              # 响应式界面样式
│  ├─ app.js                 # 前端交互、API 调用与 Canvas 绘图
│  └─ favicon.svg
├─ tests/
│  ├─ test_audio.py          # 4 项音频算法测试
│  ├─ test_database.py       # 2 项数据库测试
│  └─ test_server.py         # 1 项 HTTP 端到端测试
└─ data/                     # 首次启动时自动创建或使用
   ├─ musicscope.db          # SQLite 主数据库
   ├─ uploads/               # 本地保存的上传/演示 WAV
   └─ exports/               # 服务器生成的数据库备份
```

## 7. 数据文件说明

- 主数据库：`data/musicscope.db`
- 原始 WAV：`data/uploads/`
- 服务器备份副本：`data/exports/`

JSON、CSV 和数据库备份在浏览器下载的同时，数据库备份还会在 `data/exports/` 保留一份。JSON/CSV 不包含原始 WAV 字节，因此导入记录能够查看指标，完整 JSON 能够恢复图表，但没有原音频可供播放。

不建议在程序运行时直接修改 `musicscope.db`。如需迁移整套运行数据，应同时复制数据库和 `data/uploads/`。

## 8. 输入与容量限制

- 只接受扩展名为 `.wav` 的未压缩 PCM WAV
- PCM 位深：8、16、24、32 位
- 声道数：1～8
- 原始采样率：4 kHz～384 kHz
- 单个上传音频最大 80 MB
- 每个文件最多分析前 180 秒；结果中的 `truncated` 字段会记录是否截断
- 音频至少应包含约 0.1 秒的有效采样
- JSON/CSV 导入文件最大 25 MB
- 单次最多导入 10000 条记录

MP3、M4A、AAC、FLAC 和压缩 WAV 当前不能直接分析，需先使用其他音频工具转换为 PCM WAV。

## 9. 自动化测试

双击：

```text
一键运行测试.bat
```

或执行：

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```

当前共有 7 项测试：

1. 440 Hz 合成音能够检测为接近 440 Hz，音名为 A4。
2. 内置 C 大调、120 BPM 演示音频能够提取完整特征。
3. 损坏 WAV 能够返回可读错误，而不是导致程序崩溃。
4. 一秒静音 WAV 不会导致程序崩溃，BPM 为 0、音名为“未知”，分类为“静音或近静音”。
5. 数据库插入、读取、JSON 结构往返导入、去重和删除完整闭环。
6. SQLite 在线备份文件可以重新打开。
7. HTTP 健康检查、生成演示、读取详情、Range 音频播放和 JSON 导出端点可以端到端工作；`bytes=0-99` 返回 HTTP 206、100 字节及 RIFF 文件头。

2026-08-31 本地复测结果：`Ran 7 tests in 6.799s ... OK`。

## 10. REST API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务、版本和支持格式 |
| GET | `/api/stats` | 概览统计 |
| GET | `/api/analyses` | 历史列表，可搜索和按分类筛选 |
| GET | `/api/analyses/{id}` | 完整分析详情 |
| GET | `/api/audio/{id}` | 读取已保存的本地 WAV |
| GET | `/api/export?format=json` | 导出完整 JSON |
| GET | `/api/export?format=csv` | 导出 CSV 摘要 |
| GET | `/api/database-backup` | 下载 SQLite 备份 |
| POST | `/api/analyze?filename=...` | 上传并分析 WAV |
| POST | `/api/demo` | 生成并分析演示 WAV |
| POST | `/api/import?filename=...` | 导入 JSON/CSV |
| DELETE | `/api/analyses/{id}` | 删除记录及本地音频 |

## 11. 已知限制

- 纯 Python FFT、自相关和音高检测强调可读性与零依赖，速度和鲁棒性不等同于专业音频库。
- BPM 对弱节拍、自由速度、复杂复节奏或极短音频可能产生半拍/双拍误差。
- 单一“主导基频”更适合单音或旋律明显的素材；复杂和弦、打击乐和噪声会降低准确度。
- 调性采用平均 Chroma 与模板相关，转调、无调性或强打击乐素材可能误判。
- dBFS 是数字满刻度相对值，不是经过声压计校准的物理声压级，也不是广播标准 LUFS。
- 规则匹配度用于解释分类规则，不是统计意义上的机器学习概率。
- CSV 仅保存摘要字段，重新导入后没有波形、能量、频谱和 Chroma 序列。
- JSON/CSV 与 SQLite 备份均不打包原始音频；导入记录通常不能试听。
- 默认服务没有用户账户和鉴权，只应在可信本机使用；默认监听 `127.0.0.1`。

## 12. 隐私与安全

- 默认仅在 `127.0.0.1` 提供服务，音频不会上传到互联网。
- 上传文件名会去除目录部分并替换 Windows 非法字符。
- 静态资源路径经过目录边界检查。
- SQL 参数使用占位符，不拼接用户输入。
- HTTP 响应包含 `nosniff`、禁止 iframe 和无引用来源等安全头。
- 上传、导入和记录数量均设置上限。
