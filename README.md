# download-center

文件下载托管仓库。下载文件放在 GitHub Releases 中。

## 可靠上传与下载校验

使用 Python 3 标准库工具 `tools/release_upload.py`。凭据从 `GH_TOKEN`、`GITHUB_TOKEN` 或已有的 `gh auth login` 会话读取，不要把令牌写进脚本或聊天。令牌需要目标仓库的 `Contents: write` 权限。

```sh
python3 tools/release_upload.py \
  --repo aoyexiaoqi/download-center \
  --tag YOUR_RELEASE_TAG \
  --publish --verify-public \
  /absolute/path/to/file.zip
```

工具先确认源文件存在，再查找或创建草稿。二进制请求显式携带认证，上传后回下载，逐个核对文件大小和 SHA-256，全部通过才发布。`--verify-public` 还会验证无需登录的公开下载链接。

- 继续已有草稿：追加 `--release-id NUMERIC_ID`，并使用该草稿原有的 `--tag`。
- 暂不发布：省略 `--publish --verify-public`。草稿的公开下载地址不可用。
- 安全重试：同名文件先校验；内容不同则报错，保留原附件。
- HTTP 401：凭据缺失、过期或无效，需刷新登录或令牌。
- HTTP 403：检查仓库授权、`Contents: write`、限流和组织策略。

## 自动验证

`Verify Release upload and download` 工作流在其文件变更时执行，也可以手动运行。它使用 GitHub 自动提供的临时仓库凭据，发布一个测试预发行版，上传测试文本和上传工具本身，再匿名下载并比对 SHA-256。测试预发行版不会替换已有正式版本，也不代表其他源文件已经上传。
