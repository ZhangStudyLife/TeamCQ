# 课表空闲查询系统

这是一个纯 Python 标准库实现的局域网课表查询服务，适合直接跑在飞牛OS/NAS 上。

## 功能

- 自动扫描当前目录或指定目录下的 PDF 课表
- 解析周一到周日、1-12 节课程信息
- 识别 `2-17周`、`7-9周(单)`、`6-8节` 这类周次/节次表达式
- 支持“草稿导入 -> 管理员确认 -> 当前生效版本”
- 支持按 `日期` 或 `周次 + 星期 + 时间段` 查询谁有空
- 支持基础自然语言查询，本地规则优先，配置 OpenAI 兼容接口后可做兜底解析

## 运行

```bash
python app.py --host 127.0.0.1 --port 8123 --pdf-dir "." --state-dir ".schedule_state"
```

浏览器访问：

```text
http://127.0.0.1:8123
```

管理员后台：

```text
http://127.0.0.1:8123/admin/imports
```

如果需要开放给局域网其他设备：

```bash
python app.py --host 0.0.0.0 --port 8123
```

首次启动管理员密码：

```text
未设置 `SCHEDULE_ADMIN_PASSWORD` 时，系统会自动生成一个随机密码并打印到控制台
```

推荐通过环境变量覆盖：

```bash
set SCHEDULE_ADMIN_PASSWORD=你的密码
set SEMESTER_START_DATE=2026-03-02
python app.py
```

## 安全加固

- 管理员后台已启用会话过期、`SameSite=Strict` Cookie、CSRF 校验、登录限流、后台操作限流
- 匿名自然语言查询默认只走本地规则，不会直接消耗外部模型额度
- 可在后台设置 `管理员 IP 白名单`，例如 `127.0.0.1,192.168.1.10,192.168.1.0/24`
- 如果你只自己用，优先保持 `--host 127.0.0.1`
- 如果必须开放到局域网，务必修改管理员密码，并限制后台 IP 白名单

## 可选模型配置

后台可以保存下列 OpenAI 兼容参数：

- `llm_base_url`
- `llm_api_key`
- `llm_model`
- `vision_model`

当前版本已经接入：

- 自然语言查询的模型兜底解析

当前版本仅预留：

- 视觉模型复核配置入口

## 导入流程

1. 把 PDF 放到监控目录
2. 服务自动生成草稿版本
3. 管理员进入后台检查导入结果
4. 点击“确认该版本为当前生效课表”
5. 局域网用户开始查询

## 一次性扫描验证

```bash
python app.py --scan-only
```

## GitHub Pages 部署

GitHub Pages 只能托管静态页面，不能直接运行这个 Python 后端，也不能安全保存智谱 API Key。

因此当前仓库采用双轨模式：

- 本地版：继续负责扫描 PDF、管理员确认、模型配置
- Pages 版：只发布 `docs/` 下的静态查询页和导出的 `docs/data/schedule-data.json`

导出静态数据：

```bash
python export_pages.py --state-dir ".schedule_state" --out-dir "docs/data"
```

一键发布脚本：

```powershell
powershell -ExecutionPolicy Bypass -File ".\publish_github_pages.ps1" -RepoUrl "https://github.com/<你的用户名>/<仓库名>.git"
```

这个脚本会做 4 件事：

1. 导出静态课表数据到 `docs/data/schedule-data.json`
2. 初始化本地 Git 仓库（如果还没有）
3. 提交 `docs/`、工作流和静态资源
4. 如果你提供了 `RepoUrl`，就推送到 GitHub

### 不上传什么

- 不上传 `llm_api_key`
- 不上传管理员密码哈希
- 不上传 `.schedule_state/` 运行状态目录
- 不上传原始 PDF

### PDF 放哪里

建议把原始 PDF 长期放在本机或 NAS 的私有目录，例如：

```text
D:/Downloads/Documents/private_pdfs/
```

然后让本地版继续读取那个目录。GitHub Pages 只依赖已经导出的 JSON 数据，不需要 PDF 原件。
