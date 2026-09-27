# sessions：会话文件

一个会话一个 JSON 文件，保存恢复会话所需的全部状态，也是沉淀导师风格的原始数据。

```text
sessions/
└── <tenant_id>/
    └── <user_id>/
        └── <session_id>.json    例如 20260928-093000-a1b2c3.json
```

## 数据流

```text
启动 / /new ──▶ 新会话（只在内存）
每轮结束（成功或失败）──▶ 整份重写 <session_id>.json（先写 .tmp 再原子替换）
/sessions ──▶ 读取本用户全部会话文件，按 updated_at 从新到旧列出
/resume ──▶ 读回文件 ──▶ 装回对话历史、历史归档、审阅状态 ──▶ 与工作区文档当前内容同步
```

- 没有进行过任何一轮对话的会话不会生成文件。
- `schema_version` 与代码不一致的文件拒绝加载。
- 本目录只有这份 README 纳入版本管理，会话文件不提交。

## 示例

以下是一个进行了两轮的会话：第 1 轮读文档、记了一条问题，读取正文较长，已被压缩归档；第 2 轮用户拒绝了这条问题并给出要求。数值为示意。

```json
{
  "schema_version": 2,
  "id": "20260928-093000-a1b2c3",
  "title": "帮我看一下第二章的实验部分",
  "created_at": "2026-09-28T09:30:00+08:00",
  "updated_at": "2026-09-28T09:41:27+08:00",
  "turn_count": 2,
  "state": {
    "messages": [
      {
        "role": "user",
        "content": "F1 不用改，导师要求实验结论保留“显著”这类表述"
      },
      {
        "role": "assistant",
        "content": "",
        "tool_calls": [
          {
            "id": "call_02",
            "type": "function",
            "function": {
              "name": "update_review_state",
              "arguments": "{\"operations\": [{\"op\": \"set_status\", \"finding_id\": \"F1\", \"status\": \"rejected\"}, {\"op\": \"add_decision\", \"text\": \"实验结论保留“显著”等定性表述，不强制改为具体数值\"}]}"
            }
          }
        ]
      },
      {
        "role": "tool",
        "tool_call_id": "call_02",
        "content": "{\"ok\": true, \"results\": [{\"op\": \"set_status\", \"finding_id\": \"F1\"}, {\"op\": \"add_decision\"}]}"
      },
      {
        "role": "assistant",
        "content": "已记录：F1 保留原表述，后续审阅会遵守这一要求。",
        "reasoning_content": ""
      }
    ],
    "context": {
      "archive": {
        "entries": [
          {
            "number": 1,
            "user": "帮我看一下第二章的实验部分",
            "assistant": "第二章实验部分发现 1 处问题：F1 结论表述缺少数据支撑。",
            "tools": [
              "read_document 成功, path=论文.docx, revision=90ee57f52dcac8f08f83eec56a79c3852fefd17d531df755733edb87754075a2, 范围=p-1a2b3c40 ~ p-1a2b3c69（42 块）",
              "update_review_state 成功, 操作=mark_reviewed; add_finding F1"
            ]
          }
        ],
        "omitted_count": 0,
        "next_number": 2
      },
      "review_state": {
        "versions": {
          "论文.docx": {
            "latest_revision": "90ee57f52dcac8f08f83eec56a79c3852fefd17d531df755733edb87754075a2",
            "seen_revision": "90ee57f52dcac8f08f83eec56a79c3852fefd17d531df755733edb87754075a2",
            "latest_sequence": 1,
            "sequence_by_revision": {
              "90ee57f52dcac8f08f83eec56a79c3852fefd17d531df755733edb87754075a2": 1
            },
            "block_change_sequence": {},
            "structure_change_sequence": 0,
            "unknown_change_sequence": 0
          }
        },
        "reviewed": {
          "论文.docx": {
            "p-1a2b3c40": "3f9c0e7a1b2d4c5e",
            "p-1a2b3c41": "8e21d0c4a97b6f13"
          }
        },
        "findings": [
          {
            "id": "F1",
            "path": "论文.docx",
            "block_id": "p-1a2b3c41",
            "excerpt": "与基线方法相比，本文方法在三个数据集上均取得了显著提升。",
            "issue": "“显著提升”缺少数据支撑，建议给出具体幅度，如“平均提升 4.2%”。",
            "status": "rejected"
          }
        ],
        "decisions": [
          "实验结论保留“显著”等定性表述，不强制改为具体数值"
        ],
        "next_finding_number": 2
      }
    }
  }
}
```

## 字段说明

### 顶层

| 键 | 含义 |
|---|---|
| `schema_version` | 文件格式版本，当前为 2；与代码不一致时拒绝加载 |
| `id` | 会话 ID，时间码格式 `YYYYMMDD-HHMMSS-6位十六进制`，与文件名一致 |
| `title` | 会话标题，取首条用户输入压成的单行，用于 `/sessions` 列表 |
| `created_at` | 会话创建时间，带时区的 ISO 8601 |
| `updated_at` | 最后保存时间；`/sessions` 按它从新到旧排序 |
| `turn_count` | 成功完成的对话轮数；失败的轮次会保存状态但不计数 |
| `state` | 会话状态，`/resume` 时整体装回运行时；含 `messages` 与 `context` |

### state.messages[]：压缩后的对话历史

OpenAI Chat Completions 消息格式。已归档轮次不在这里（见 `archive`），历史中 `read_document` 读到的正文可能已被压缩清理为占位说明。

| 键 | 含义 |
|---|---|
| `role` | `user` 用户输入；`assistant` 模型回复；`tool` 工具返回结果 |
| `content` | 文本内容；`tool` 消息为工具返回结果的 JSON 字符串；发起工具调用的 `assistant` 消息可能为空 |
| `tool_calls` | 仅 `assistant`：本次发起的工具调用列表 |
| `tool_calls[].id` | 工具调用 ID，与对应 `tool` 消息的 `tool_call_id` 配对 |
| `tool_calls[].type` | 固定为 `function` |
| `tool_calls[].function.name` | 工具名 |
| `tool_calls[].function.arguments` | 调用参数，JSON 字符串 |
| `tool_call_id` | 仅 `tool`：该结果对应的工具调用 ID |
| `reasoning_content` | 仅 `assistant`：模型返回的推理内容（Kimi 等模型的字段）；`assistant` 消息按模型响应原样保存，换模型可能出现其它字段 |

### state.context.archive：历史归档

早期轮次从 `messages` 中移出后只保留摘要条目，渲染进系统提示词。

| 键 | 含义 |
|---|---|
| `entries` | 仍在摘要中展示的轮次，按时间排序 |
| `entries[].number` | 归档轮次编号，从 1 开始 |
| `entries[].user` | 该轮用户输入 |
| `entries[].assistant` | 该轮最终回复 |
| `entries[].tools` | 该轮每次工具调用的事实记录，一条一行：工具名、成败、路径、revision、块范围、操作 |
| `omitted_count` | 摘要超出上限后被整条丢弃的最早轮次数 |
| `next_number` | 下一个归档轮次的编号 |

### state.context.review_state：审阅状态

路径均相对用户工作区。块 ID：段落有 Word paraId 时为 `p-<paraId>`，插入删除后不变；否则为 `p-<序号>` 或表格 `t-<序号>`，插入删除后可能错位。

| 键 | 含义 |
|---|---|
| `versions` | 文档路径 → 该文档在本会话中的版本记录 |
| `versions.<path>.latest_revision` | 最新已知的 revision（文件内容 SHA-256） |
| `versions.<path>.seen_revision` | 模型最后读到或自己修改后得到的 revision；与 `latest_revision` 不同说明文档在会话外被改过、模型尚未重读 |
| `versions.<path>.latest_sequence` | 最新 revision 在本会话中的序号，从 1 开始 |
| `versions.<path>.sequence_by_revision` | revision → 序号 |
| `versions.<path>.block_change_sequence` | 块 ID → 该块最后一次被修改时的序号，用于判断历史中的旧读取是否过时 |
| `versions.<path>.structure_change_sequence` | 最后一次插入或删除块时的序号，0 表示没有发生过 |
| `versions.<path>.unknown_change_sequence` | 最后一次发现会话外修改时的序号，0 表示没有发生过 |
| `reviewed` | 文档路径 → {已审阅块 ID: 块正文指纹} |
| `reviewed.<path>.<block_id>` | 标记已审阅时该块正文 SHA-256 的前 16 位；正文变化后该标记失效 |
| `findings` | 审阅问题列表 |
| `findings[].id` | 会话内问题编号，F1、F2… |
| `findings[].path` | 问题所在文档 |
| `findings[].block_id` | 问题所在内容块 ID |
| `findings[].excerpt` | 记录问题时该块的原文（接受修订视图），文档之后被改也保留，供沉淀导师风格 |
| `findings[].issue` | 问题描述与修改建议 |
| `findings[].status` | `open` 待处理；`accepted` 用户接受；`rejected` 用户拒绝；`resolved` 已修改 |
| `decisions` | 用户明确表达的审阅决定或偏好，按记录顺序 |
| `next_finding_number` | 下一个问题编号的序号 |
