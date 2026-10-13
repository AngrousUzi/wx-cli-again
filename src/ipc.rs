use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// CLI 向 daemon 发送的请求（换行符分隔 JSON，与 Python 版兼容）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    Ping,
    Sessions {
        #[serde(default = "default_limit_20")]
        limit: usize,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    History {
        chat: String,
        #[serde(default = "default_limit_50")]
        limit: usize,
        #[serde(default)]
        offset: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        /// 游标：create_time < after_ts（向更旧翻页，传上一页最旧一条的 timestamp）
        #[serde(default, skip_serializing_if = "Option::is_none")]
        after_ts: Option<i64>,
        /// 游标：create_time > before_ts（向更新翻页）
        #[serde(default, skip_serializing_if = "Option::is_none")]
        before_ts: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        msg_type: Option<i64>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    Search {
        keyword: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        chats: Option<Vec<String>>,
        #[serde(default = "default_limit_20")]
        limit: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        msg_type: Option<i64>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    Contacts {
        #[serde(skip_serializing_if = "Option::is_none")]
        query: Option<String>,
        #[serde(default = "default_limit_50")]
        limit: usize,
    },
    Unread {
        #[serde(default = "default_limit_20")]
        limit: usize,
        /// 按会话类型过滤：private / group / official / folded / all，支持多选
        #[serde(default, skip_serializing_if = "Option::is_none")]
        filter: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    Members {
        chat: String,
    },
    NewMessages {
        /// 上次检查时各会话的 last_timestamp 快照（username -> ts）
        /// None 表示首次运行，会返回 new_state 供下次使用
        #[serde(skip_serializing_if = "Option::is_none")]
        state: Option<HashMap<String, i64>>,
        #[serde(default = "default_limit_200")]
        limit: usize,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    Stats {
        chat: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    Favorites {
        #[serde(default = "default_limit_50")]
        limit: usize,
        /// 类型过滤：1=文本,2=图片,5=文章,19=名片,20=视频
        #[serde(skip_serializing_if = "Option::is_none")]
        fav_type: Option<i64>,
        /// 内容关键词搜索
        #[serde(skip_serializing_if = "Option::is_none")]
        query: Option<String>,
    },
    /// 朋友圈互动通知（点赞 + 评论）
    SnsNotifications {
        #[serde(default = "default_limit_50")]
        limit: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        /// 包含已读通知（默认仅未读）
        #[serde(default)]
        include_read: bool,
    },
    /// 朋友圈时间线（按时间 / 作者筛选帖子）
    SnsFeed {
        #[serde(default = "default_limit_20")]
        limit: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        /// 作者昵称 / 备注名 / 微信 username，模糊匹配
        #[serde(skip_serializing_if = "Option::is_none")]
        user: Option<String>,
    },
    /// 查询公众号文章推送（biz_message_*.db 分片）
    BizArticles {
        #[serde(default = "default_limit_50")]
        limit: usize,
        /// 公众号名称过滤（模糊匹配 display name，None = 全部）
        #[serde(skip_serializing_if = "Option::is_none")]
        account: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        /// 只看有未读消息的公众号，每个公众号取最新 1 篇
        #[serde(default)]
        unread: bool,
    },
    /// 调试用：dump 一条或多条 SnsTimeLine 的原始 XML，用于核对 schema
    /// （likeUserList / commentUserList 真实标签名等）。隐藏命令，不进 README。
    SnsRaw {
        /// 指定 tid；None 表示按 tid DESC 取最近 N 条
        #[serde(skip_serializing_if = "Option::is_none")]
        tid: Option<i64>,
        #[serde(default = "default_limit_5")]
        limit: usize,
    },
    /// 朋友圈全文搜索（匹配 contentDesc）
    SnsSearch {
        keyword: String,
        #[serde(default = "default_limit_20")]
        limit: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        user: Option<String>,
    },
    /// 重新加载配置和密钥（key extract / key set 后 daemon 不会自动重读时用）
    ReloadConfig,
    /// 列出某个会话里的图片附件
    /// 输出每条带 `attachment_id`（不透明 base64url 句柄），传给 `Extract` 时取回本体
    Attachments {
        chat: String,
        /// 类型过滤：当前仅支持 image
        #[serde(default, skip_serializing_if = "Option::is_none")]
        kinds: Option<Vec<String>>,
        #[serde(default = "default_limit_50")]
        limit: usize,
        #[serde(default)]
        offset: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
    /// 提取（解密）单个附件的本体到指定路径
    Extract {
        /// `Attachments` 返回的不透明 ID
        attachment_id: String,
        /// 写入的绝对路径（daemon 直接写盘，不经 socket 传 binary）
        output: String,
        /// 已存在时是否覆盖
        #[serde(default)]
        overwrite: bool,
    },
    /// 跨会话时间线（按时间合并多 chat 消息）
    Timeline {
        #[serde(default = "default_limit_50")]
        limit: usize,
        #[serde(default)]
        offset: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        since: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        until: Option<i64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        after_ts: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        msg_type: Option<i64>,
        #[serde(default, skip_serializing_if = "is_false")]
        with_meta: bool,
        #[serde(default, skip_serializing_if = "is_false")]
        debug_source: bool,
    },
}

/// daemon 的响应
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(flatten)]
    pub data: Value,
}

impl Response {
    pub fn ok(data: Value) -> Self {
        Self {
            ok: true,
            error: None,
            data,
        }
    }

    pub fn err(msg: impl Into<String>) -> Self {
        Self {
            ok: false,
            error: Some(msg.into()),
            data: Value::Null,
        }
    }

    pub fn to_json_line(&self) -> anyhow::Result<String> {
        let s = serde_json::to_string(self)?;
        Ok(s + "\n")
    }
}

/// 可执行文件指纹。daemon 启动时记下自身二进制的指纹，Ping 时回传；
/// CLI 发现磁盘上同一路径的文件已变（重新编译 / 安装覆盖）就重启 daemon。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExeStamp {
    pub path: PathBuf,
    pub len: u64,
    pub mtime_ns: u64,
}

impl ExeStamp {
    pub fn of(path: &Path) -> Option<Self> {
        let meta = std::fs::metadata(path).ok()?;
        let mtime = meta.modified().ok()?.duration_since(UNIX_EPOCH).ok()?;
        Some(Self {
            path: path.to_path_buf(),
            len: meta.len(),
            mtime_ns: mtime.as_nanos() as u64,
        })
    }

    pub fn current() -> Option<Self> {
        Self::of(&std::env::current_exe().ok()?)
    }
}

fn default_limit_20() -> usize {
    20
}
fn default_limit_50() -> usize {
    50
}
fn default_limit_200() -> usize {
    200
}
fn default_limit_5() -> usize {
    5
}
fn is_false(v: &bool) -> bool {
    !*v
}
