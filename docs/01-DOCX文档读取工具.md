# DOCX 文档读取工具

本文说明 `ReadDocumentTool` 的用途、读取流程、输入参数、返回 JSON、定位 ID、使用方式和当前限制，供 Runtime 接入、修改工具开发及问题排查使用。

## 1 工具定位

`ReadDocumentTool` 是一个只读工具，负责将租户工作目录中的 `.docx` 文档转换为适合大模型阅读的 Markdown，并同时返回可供后续修改工具定位 OOXML 节点的结构索引。

工具实现按职责拆分为：

```text
backend/tools/read_document/
├── __init__.py  # 对外导出 ReadDocumentTool
├── tool.py      # 参数校验、访问边界、分块和返回结果组装
├── parser.py    # OOXML 内容解析
└── models.py    # 内部文档块模型及 Markdown/索引转换
```

当前实现直接使用 `lxml` 解析 DOCX 内部的 OOXML，不依赖 Microsoft Word，也不会把 DOCX 解压到项目目录。

它的主要目标是：

1. 保持正文中段落、表格和内容控件的原始顺序。
2. 为模型展示带定位 ID 的 Markdown。
3. 返回不重复正文的轻量结构索引，供后续修改工具定位。
4. 识别标题、列表、表格、图片占位、批注和修订痕迹。
5. 支持标题大纲和按 ID 分块续读，避免长文档一次占满模型上下文。
6. 限制工具能够访问的根目录，避免模型读取租户目录之外的文件。

## 2 DOCX 的读取基础

DOCX 本质上是一个 ZIP 包，内部包含多个 XML 和媒体文件。当前工具主要读取以下部件：

| DOCX 部件 | 用途 |
| --- | --- |
| `word/document.xml` | 正文段落、表格、图片引用和修订 |
| `word/styles.xml` | 段落样式、标题层级和样式内列表设置 |
| `word/numbering.xml` | 有序列表和无序列表的编号格式 |
| `word/comments.xml` | 批注作者与批注文本 |
| `word/header*.xml` | 页眉文本 |
| `word/footer*.xml` | 页脚文本 |
| `word/footnotes.xml` | 脚注文本 |
| `word/endnotes.xml` | 尾注文本 |
| `word/media/*` | 图片等媒体文件；当前只识别其存在，不解析视觉内容 |

## 3 完整读取流程

### 3.1 初始化访问范围

创建工具时必须传入允许读取的根目录：

```python
from pathlib import Path

from backend.tools import ReadDocumentTool

tool = ReadDocumentTool(
    Path("backend/data_agent/tenant_packs/233/dbh")
)
```

工具会立即解析根目录的真实路径。目录不存在或不是目录时抛出 `NotADirectoryError`。

根目录由项目代码注入，不应由模型决定。模型只需要传递根目录内的相对路径，例如 `PE.docx`。

### 3.2 校验目标路径

执行工具时会依次检查：

1. `path` 必须是非空字符串。
2. 相对路径基于允许根目录解析。
3. 绝对路径也必须仍然位于允许根目录内。
4. 解析 `..` 和符号链接后的真实路径不能越过根目录。
5. 文件扩展名必须是 `.docx`。
6. 目标必须是实际存在的文件。

路径越界会抛出 `PermissionError`，其他无效路径会抛出 `ValueError` 或 `FileNotFoundError`。

### 3.3 安全解析 OOXML

工具使用以下限制创建 XML 解析器：

```text
resolve_entities = False
no_network       = True
load_dtd         = False
huge_tree        = False
recover          = False
```

这意味着解析器不会解析外部实体、访问网络或加载外部 DTD。单个 XML 部件的未压缩大小上限为 50 MiB，超过时抛出 `ValueError`。

### 3.4 加载样式、编号和批注

正文解析之前，工具会建立三个辅助索引：

- 样式索引：`styleId -> (显示名, 标题层级)`。
- 编号索引：`numId -> {列表层级: 编号格式}`。
- 批注索引：`commentId -> {作者, 文本}`。

标题层级优先读取段落自身的 `w:outlineLvl`，其次读取样式中的大纲层级，最后才根据 `Heading 1`、`标题 1` 等样式名称推断。

列表设置优先读取段落自身的 `w:numPr`。段落没有直接编号设置时，再检查样式中是否定义了编号。

### 3.5 按正文顺序解析内容块

工具按 `word/document.xml` 中 `w:body` 的子节点顺序遍历：

- `w:p` 转换为段落、标题或列表项。
- `w:tbl` 转换为 Markdown 表格。
- `w:sdt` 转换为标记为 `content-control` 的普通内容块。
- `w:sectPr` 等非内容节点不会形成内容块。

没有正文、批注或修订信息的普通空段落会被过滤，不占用模型上下文。因此返回的 `block_count` 不等于 Word 内部所有 `w:p` 节点数量。

### 3.6 生成稳定定位 ID

普通段落优先使用 Word 自带的 `w14:paraId`：

```text
w14:paraId="59F2D0B6"
        ↓
p-59f2d0b6
```

如果段落没有 `w14:paraId`，则使用正文 XML 位置回退：

```text
p-10
```

表格和内容控件目前没有等价的原生稳定 ID，因此使用正文 XML 位置：

```text
t-15
content-20
```

位置型 ID 只在同一个 `revision` 对应的文档快照中可靠。如果文档结构发生变化，XML 位置可能改变。后续修改工具必须同时校验 `revision`，不能只依赖 ID。

### 3.7 提取正文文本

文本提取会处理：

- 普通文本。
- 制表符和换行符。
- 图片占位符，例如 `[图片：image2.png]`。
- 脚注引用，例如 `[脚注:0]`。
- 尾注引用，例如 `[尾注:1]`。
- Word 数学公式中的可见文本。
- 插入和删除修订。

工具支持两种修订视图：

| `mode` | 行为 |
| --- | --- |
| `accepted` | 保留插入内容，忽略删除内容，生成接受修订后的阅读视图 |
| `markup` | 使用 `{+插入内容+}` 和 `[-删除内容-]` 显式展示修订 |

### 3.8 解析表格

表格转换为 Markdown 表格，并进行以下处理：

- `w:gridSpan` 横向合并单元格会补充空列，保持列对齐。
- `w:vMerge` 纵向合并的延续单元格使用 `〃` 表示。
- 单元格内多个段落使用 `<br>` 连接。
- 单元格中的 `|` 会转义为 `\|`。
- 第一行被用作 Markdown 表头。

### 3.9 关联批注

工具读取段落内的 `commentRangeStart` 和 `commentReference`，按照 XML 出现顺序去重，再将批注附加到对应段落 Markdown 后面：

```markdown
[p-1c2d7be7] 段落正文
  > 批注（作者）：批注内容
```

完整批注列表也会单独出现在返回 JSON 的 `comments` 字段中。

### 3.10 分块与续读

默认单次 Markdown 字符上限是 20000，可通过 `max_chars` 调整，允许范围为 1000 到 100000。

工具按完整内容块截断，不会从普通段落中间切断。因此实际返回字符数通常小于上限；如果单个内容块本身超过上限，为保证能够继续前进，该完整内容块仍会被返回。

存在后续内容时，返回：

```json
{
  "next_id": "p-229fc8d4",
  "metadata": {
    "truncated": true
  }
}
```

下一次读取时传入：

```python
next_result = tool.execute(
    path="PE.docx",
    start_id=result["next_id"],
    max_chars=20000,
)
```

`next_id` 为 `null` 时表示当前视图已经读取完毕。

## 4 输入参数

### 4.1 参数总览

| 参数 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `path` | `string` | 是 | 无 | 根目录内的 DOCX 相对路径，或仍位于根目录内的绝对路径 |
| `mode` | `string` | 否 | `accepted` | 修订展示方式，可选 `accepted`、`markup` |
| `view` | `string` | 否 | `content` | 读取正文或标题大纲，可选 `content`、`outline` |
| `start_id` | `string` | 否 | `null` | 从指定内容块 ID 开始续读 |
| `max_chars` | `integer` | 否 | `20000` | 单次 Markdown 字符上限，范围 1000 到 100000 |

### 4.2 正文读取示例

```python
result = tool.execute(
    path="PE.docx",
    mode="accepted",
    view="content",
    max_chars=5000,
)
```

### 4.3 修订读取示例

```python
result = tool.execute(
    path="PE.docx",
    mode="markup",
)
```

### 4.4 标题大纲示例

```python
result = tool.execute(
    path="PE.docx",
    view="outline",
)
```

`outline` 视图仍返回相同的 JSON 外层结构，但 `markdown` 和 `index` 只包含标题块。

## 5 返回 JSON 总览

典型返回结构如下：

```json
{
  "path": "PE.docx",
  "revision": "64位SHA-256字符串",
  "mode": "accepted",
  "view": "content",
  "markdown": "[p-59afad81] 文档内容……",
  "index": [
    {
      "id": "p-59afad81",
      "kind": "paragraph",
      "level": 0,
      "style": "Title",
      "para_id": "59AFAD81",
      "xml_index": 0,
      "has_changes": false,
      "comment_count": 0
    }
  ],
  "next_id": "p-229fc8d4",
  "extras": {
    "headers": [
      {
        "part": "word/header1.xml",
        "text": "页眉文本"
      }
    ],
    "footnotes": [
      {
        "id": "0",
        "part": "word/footnotes.xml",
        "text": "脚注文本"
      }
    ]
  },
  "comments": [
    {
      "id": "0",
      "author": "作者",
      "text": "批注文本"
    }
  ],
  "metadata": {
    "block_count": 166,
    "returned_block_count": 22,
    "heading_count": 29,
    "table_count": 0,
    "changed_block_count": 0,
    "section_count": 2,
    "drawing_count": 4,
    "truncated": true
  },
  "warnings": [
    "图片仅以位置占位符呈现，尚未识别图片视觉内容"
  ]
}
```

## 6 返回字段详细说明

### 6.1 外层字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `path` | `string` | 相对于允许根目录的规范化文档路径，不返回根目录外的绝对路径 |
| `revision` | `string` | DOCX 整体二进制内容的 SHA-256，用于后续修改前检查文档是否发生变化 |
| `mode` | `string` | 本次使用的修订视图，即 `accepted` 或 `markup` |
| `view` | `string` | 本次读取正文还是标题大纲，即 `content` 或 `outline` |
| `markdown` | `string` | 带内容块 ID 的模型阅读文本 |
| `index` | `array` | 本次返回内容块的轻量定位索引，不重复保存正文文本 |
| `next_id` | `string \| null` | 下一次续读的起始内容块 ID；为 `null` 表示已经读完当前视图 |
| `extras` | `object` | 页眉、页脚、脚注和尾注等正文外内容 |
| `comments` | `array` | 文档内所有已读取批注的完整列表 |
| `metadata` | `object` | 整篇文档及本次返回结果的统计信息 |
| `warnings` | `array` | 当前读取器无法完整解释的内容或降级处理说明 |

### 6.2 `index` 字段

`index` 只包含本次 `markdown` 中返回的内容块。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | `string` | 模型引用内容块使用的 ID，例如 `p-59afad81`、`t-15` |
| `kind` | `string` | `paragraph`、`heading`、`list_item` 或 `table` |
| `level` | `integer` | 标题级别或列表缩进层级；普通段落和表格为 0 |
| `style` | `string` | Word 样式显示名；列表项则为 `ordered` 或 `bullet` |
| `para_id` | `string` | Word 原生 `w14:paraId`；表格和没有原生 ID 的内容为空字符串 |
| `xml_index` | `integer` | 内容块在 `w:body` 子节点中的位置，供修改工具在同一 revision 内回查 |
| `has_changes` | `boolean` | 内容块是否包含 `w:ins` 或 `w:del` 修订节点 |
| `comment_count` | `integer` | 当前内容块关联的批注数量 |

### 6.3 `extras` 字段

`extras` 按实际存在的部件动态生成键，没有对应内容时不会返回空键。

| 键 | 内容 |
| --- | --- |
| `headers` | 页眉部件路径和可见文本 |
| `footers` | 页脚部件路径和可见文本 |
| `footnotes` | 非系统保留脚注的 ID、部件路径和文本 |
| `endnotes` | 非系统保留尾注的 ID、部件路径和文本 |

Word 会在脚注和尾注 XML 中保存负数 ID 的系统分隔项。工具会过滤这些系统项，只返回 ID 大于或等于 0 且包含文本的实际内容。

### 6.4 `comments` 字段

每条批注包含：

| 字段 | 含义 |
| --- | --- |
| `id` | Word 批注 ID |
| `author` | 批注作者；文档没有作者信息时为空字符串 |
| `text` | 将批注中的文本节点按顺序拼接后的内容 |

批注既会出现在完整 `comments` 列表中，也会在其关联段落的 Markdown 下方展示。

### 6.5 `metadata` 字段

| 字段 | 含义 |
| --- | --- |
| `block_count` | 过滤普通空段落后，整篇正文生成的内容块数量 |
| `returned_block_count` | 本次受 `view`、`start_id` 和 `max_chars` 影响后实际返回的块数量 |
| `heading_count` | 整篇正文识别出的标题块数量 |
| `table_count` | 整篇正文识别出的顶层表格块数量 |
| `changed_block_count` | 包含插入或删除修订的正文块数量 |
| `section_count` | `document.xml` 中 `w:sectPr` 的数量，即 Word 页面布局节数量，不代表页数或论文章节数 |
| `drawing_count` | 正文中 `w:drawing` 节点数量，包括行内或浮动的 DrawingML 对象，不等同于已完成视觉解析的图片数量 |
| `truncated` | `next_id` 是否非空，即本次是否还有后续内容未返回 |

### 6.6 `warnings` 字段

当前可能返回的警告包括：

| 警告 | 含义 |
| --- | --- |
| 图片仅以位置占位符呈现 | 文档有媒体文件，但工具没有对图片进行 OCR、视觉理解或返回二进制数据 |
| 外部嵌入内容 altChunk 尚未解析 | 文档包含外部导入的 HTML 或其他嵌入内容 |
| 移动修订尚未单独标记 | 文档包含 `moveFrom` 或 `moveTo`，当前没有按移动操作呈现 |
| 公式只提取可见文本 | 检测到 Word 数学公式，但没有返回完整 OMML 数学结构 |

## 7 与 ToolEngine 配合

工具可以直接测试，也可以注册到 `ToolEngine`：

```python
from pathlib import Path

from backend.runtime import ToolEngine
from backend.tools import ReadDocumentTool

tool_engine = ToolEngine()
tool_engine.register(
    ReadDocumentTool(
        Path("backend/data_agent/tenant_packs/233/dbh")
    )
)

result = tool_engine.execute(
    "read_document",
    {
        "path": "PE.docx",
        "view": "content",
        "max_chars": 5000,
    },
)
```

通过 `ToolEngine` 执行时，默认超时时间为 300 秒。超时会抛出 `TimeoutError`。

## 8 独立测试

测试文件：

```text
tests/test_read_document.py
```

执行：

```powershell
uv run pytest tests/test_read_document.py -s -q
```

该测试不创建模型客户端，也不会调用任何大模型 API。它直接读取 `PE.docx`，打印元数据、索引、批注、附加部件、警告、续读 ID 和 Markdown。

## 9 当前不足

### 9.1 文件格式

- 当前只支持 `.docx`，不支持旧版二进制 `.doc`。
- `.doc` 后续需要先通过独立转换器转换为 `.docx`，不应直接混入当前 OOXML 解析逻辑。

### 9.2 图片和复杂对象

- 图片只生成位置占位符，不读取像素内容，不进行 OCR 或视觉理解。
- 尚未为图片返回关系 ID、媒体文件路径、尺寸和锚定方式等完整索引。
- 图表、SmartArt、OLE 嵌入对象、文本框和 `altChunk` 尚未结构化解析。
- 图片可能出现在页眉、页脚或复杂绘图容器中，当前占位符主要覆盖正文 XML 中能够发现的绘图节点。

### 9.3 公式

- Word OMML 公式目前只拼接可见文本。
- 返回结果不能完整表达分数、上下标、矩阵、根式等数学结构。
- 后续需要决定返回 OMML、转换为 LaTeX，还是同时提供两种形式。

### 9.4 表格

- Markdown 表格默认把第一行当作表头，即使 Word 中没有真正设置表头。
- 横向和纵向合并使用补空格与 `〃` 提示，不能完整表达复杂跨行跨列结构。
- 嵌套表格中的段落目前会被拍平成单元格文本，不会返回独立的嵌套表格块。
- 表格自身没有 Word 原生稳定 ID，当前依赖 `xml_index` 和 `revision` 定位。

### 9.5 列表

- 工具能区分有序和无序列表，但不会计算实际显示的编号值。
- 有序列表统一输出 Markdown `1.`，交给 Markdown 渲染器自动编号。
- 复杂的重新开始编号、编号覆盖和多级模板可能无法完全还原。

### 9.6 批注和修订

- 批注当前关联到包含开始标记或引用标记的段落，没有完整建立跨段落批注范围。
- 批注回复、线程关系、日期和解决状态尚未结构化表示。
- 支持普通插入和删除修订，但 `moveFrom`、`moveTo` 等移动修订只会返回警告。
- 修订作者、时间和修订 ID 尚未返回。

### 9.7 超链接、域和引用

- 超链接的可见文本可以被读取，但当前不会返回 URL 和关系 ID。
- 目录、页码、交叉引用等 Word 域主要依赖文档缓存的可见文本，没有单独返回域代码。
- 内容控件会被拍平成普通文本块，不会返回控件标签、绑定数据和可编辑范围。

### 9.8 页面布局

- 工具读取的是 OOXML 逻辑结构，不计算内容位于第几页。
- 不检测文字重叠、图片漂移、表格截断或分页异常。
- 文档修改完成后的视觉质量必须通过 DOCX 渲染和页面图片检查确认，不能只依赖当前读取结果。

### 9.9 分块和性能

- 每次使用 `start_id` 续读时都会重新解析完整 DOCX，目前没有缓存解析结果。
- `max_chars` 以完整内容块为边界，单个超大表格或段落可能使实际返回长度超过上限。
- 当前只限制单个 XML 部件大小，没有对 ZIP 包内所有部件的累计解压大小设置总上限。
- SHA-256 `revision` 在解析完成后计算，当前没有文件锁；外部程序并发修改文档时仍需要修改工具再次校验 revision。

## 10 后续修改工具的使用约束

后续 `EditDocumentTool` 应至少接收：

```json
{
  "path": "PE.docx",
  "revision": "读取时返回的SHA-256",
  "operations": [
    {
      "block_id": "p-59f2d0b6",
      "operation": "replace_text",
      "old_text": "原内容",
      "new_text": "新内容"
    }
  ]
}
```

修改前必须重新计算文档 revision。revision 不一致时应拒绝写入，要求模型重新读取，避免使用旧索引覆盖已经变化的文档。

段落应优先通过 `para_id` 回查；表格和没有原生 ID 的内容才使用 `xml_index`，且必须与 revision 一起校验。
