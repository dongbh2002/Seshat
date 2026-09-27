# workspaces：用户工作区

存放用户要处理的文档，也是 Seshat 工具唯一能访问的目录。

```text
workspaces/
└── <tenant_id>/            租户（课题组），由启动参数 --tenant 指定
    └── <user_id>/          用户，由启动参数 --user 指定；此目录即该用户的工作区
        ├── 论文.docx        用户放入的待处理文档
        └── 论文_修订.docx   Seshat 另存的修改结果（write_document 指定 output_path 时）
```

- **谁写入**：用户把待处理的 DOCX 放进自己的工作区；Seshat 修改文档时，默认以修订模式原地覆盖源文档，指定另存路径时写成新文件，同样在这里。
- **访问边界**：`read_document`、`write_document` 等工具只能访问当前用户的工作区，路径一律相对工作区填写，越界访问会被拒绝。
- **何时创建**：启动时工作区不存在就自动创建。
- **游客**：不传 `--tenant`/`--user` 时，工作区为 `guest/guest-<时间码>/`，每次启动都是新的。
- **不提交 git**：本目录只有这份 README 纳入版本管理，用户文档不提交。
