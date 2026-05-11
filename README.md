# X Timeline Agent

AI 驱动的 X（Twitter）时间线策展助手。自动抓取时间线、对推文进行价值评分、生成深度摘要、通过飞书推送日报——还内置了一个交互式聊天 Agent，可以随时回答关于时间线的问题。

## 功能特性

- **浏览器自动化** — 基于 Playwright 的 X 抓取器，内置 stealth 插件、cookie 持久化
- **AI 分类评分** — LLM 从 6 个维度给每条推文打分（信息密度、可操作见解、独特观点、经验分享、重要事件、情绪共鸣）
- **深度摘要** — 话题聚类、观点交叉对比、增量摘要（带状态持久化）
- **语义搜索** — 基于 Sentence-Transformer 的向量嵌入，支持中英双语关键词搜索
- **定时推送** — 可配置的每日飞书摘要推送（支持 Webhook 和企业 Bot）
- **交互式对话** — 10 种意图的工具调用 Agent（总结、搜索、收藏、回溯上下文、记忆管理）
- **长期记忆** — 提取用户偏好和话题快照，通过嵌入相似度去重合并

## 快速开始

### 环境要求

- Python 3.12+
- Playwright Chromium
- LLM API Key（DeepSeek 或任意 OpenAI 兼容接口）
- 飞书 Webhook URL（用于接收摘要推送）
- X（Twitter）账号

### 安装

```bash
git clone https://github.com/yunz4446-commits/x-timeline-agent.git
cd x-timeline-agent
pip install -r requirements.txt
playwright install --with-deps chromium
```

### 配置

```bash
cp .env.example .env
# 编辑 .env — 填写 LLM_API_KEY 和 FEISHU_WEBHOOK_URL
```

`config.yaml` 中可调整调度时间、评分阈值等可选参数。

### 使用

```bash
python main.py setup     # 检查配置
python main.py login     # 打开浏览器登录 X
python main.py fetch     # 手动抓取 + 分类
python main.py digest    # 发送一次测试摘要
python main.py chat      # 进入交互对话
python main.py run       # 启动定时调度 + 飞书回调服务
```

## 项目结构

```
src/
├── agent/          # 对话 Agent（核心、工具、提示词、记忆、摘要、叙事）
├── browser/        # X.com 浏览器自动化（Playwright + stealth）
├── classifier/     # LLM 推文分类评分
├── digest/         # 摘要生成 + Markdown 格式化
├── channels/       # 飞书 Webhook + 企业 Bot
├── scheduler/      # APScheduler 定时任务（抓取、分类、摘要、清理）
├── db/             # SQLAlchemy 模型 + 数据访问
└── search/         # Sentence-Transformer 嵌入 + 语义搜索
```

## License

MIT
