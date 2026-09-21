use crate::ipc::Request;
use super::transport;
use super::output::{print_value, resolve};
use anyhow::Result;

/// 隐藏调试命令：dump SnsTimeLine.content 原始 XML，用于核对 likeUserList /
/// commentUserList 等真实标签名。默认 JSON 输出，便于 `jq -r '.rows[0].content'`。
pub fn cmd_sns_raw(tid: Option<i64>, limit: usize, json: bool) -> Result<()> {
    let req = Request::SnsRaw { tid, limit };
    let resp = transport::send(req)?;
    // 默认 JSON：XML 里有大量引号和换行，JSON 转义比 YAML 多行块更稳。
    // 用户显式传 --no-json 时才走 YAML（这里 json=true 是默认值）。
    print_value(&resp.data, &resolve(json))
}
