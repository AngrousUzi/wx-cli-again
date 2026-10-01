//! Linux V2 image AES key 提取。
//! 与 macOS 同理，从 ~/.xwechat/net/kvcomm/ 下的 `key_<uin>_*.statistic` 文件名拿 uin，
//! 然后通过 `md5(str(uin) + wxid)[:16]` 派生 AES key，XOR key 为 `uin & 0xff`。

pub type LinuxImageKeyProvider = super::macos::MacosImageKeyProvider;
