/// Linux WeChat 进程内存密钥扫描器
///
/// 通过 /proc/<pid>/maps 枚举内存区域，
/// 通过 /proc/<pid>/mem 读取内存内容，
/// 搜索 x'<64hex><32hex>' 格式的 SQLCipher 密钥
use anyhow::{bail, Context, Result};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use super::{
    collect_db_salts, collect_salt_adjacent_keys, decode_salt_hex, match_key_hexes,
    scan_key_patterns, unique_key_hexes, KeyEntry, MAX_PATTERN_BYTES,
};

const CHUNK_SIZE: usize = 2 * 1024 * 1024;

/// 查找 WeChat 进程 PID
fn find_wechat_pid() -> Option<u32> {
    let proc_dir = std::fs::read_dir("/proc").ok()?;
    for entry in proc_dir.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        // 只处理数字目录（PID）
        if !name_str.chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        let comm_path = format!("/proc/{}/comm", name_str);
        if let Ok(comm) = std::fs::read_to_string(&comm_path) {
            let comm = comm.trim().to_lowercase();
            if comm == "wechat" || comm == "weixin" {
                if let Ok(pid) = name_str.parse::<u32>() {
                    return Some(pid);
                }
            }
        }
    }
    None
}

/// 解析 /proc/<pid>/maps 文件，返回可读的内存区域 (start, end)
fn parse_maps(pid: u32) -> Result<Vec<(u64, u64)>> {
    let maps_path = format!("/proc/{}/maps", pid);
    let content =
        std::fs::read_to_string(&maps_path).with_context(|| format!("读取 {} 失败", maps_path))?;

    let mut regions = Vec::new();
    for line in content.lines() {
        // 格式: start-end perms offset dev inode pathname
        let parts: Vec<&str> = line.splitn(2, ' ').collect();
        if parts.len() < 2 {
            continue;
        }
        let perms = parts[1].trim_start();
        // 只选取 r 和 w 权限的区域
        if !perms.starts_with("rw") {
            continue;
        }
        let addr_parts: Vec<&str> = parts[0].splitn(2, '-').collect();
        if addr_parts.len() != 2 {
            continue;
        }
        if let (Ok(start), Ok(end)) = (
            u64::from_str_radix(addr_parts[0], 16),
            u64::from_str_radix(addr_parts[1], 16),
        ) {
            regions.push((start, end));
        }
    }
    Ok(regions)
}

pub fn scan_keys(db_dir: &Path) -> Result<Vec<KeyEntry>> {
    let pid = find_wechat_pid().context("找不到 WeChat 进程，请确认 WeChat 正在运行")?;
    eprintln!("WeChat PID: {}", pid);

    let db_salts = collect_db_salts(db_dir);
    eprintln!("找到 {} 个加密数据库", db_salts.len());

    // salt 字节用于「salt 邻接」二进制 key 扫描：新版 Linux 微信（Radium 4.x）
    // 不再把 key 以 ASCII `x'<hex>'` 形式留在内存，只能按 DB salt 在堆上找邻接的
    // 32-byte raw key。与 macOS 扫描器同源（collect_salt_adjacent_keys）。
    let salt_bytes: Vec<[u8; 16]> = db_salts
        .iter()
        .filter_map(|(s, _)| decode_salt_hex(s))
        .collect();

    eprintln!("扫描进程内存...");
    let regions = parse_maps(pid)?;
    eprintln!("找到 {} 个可读写内存区域", regions.len());

    let mem_path = format!("/proc/{}/mem", pid);
    let mut mem_file = std::fs::File::open(&mem_path)
        .with_context(|| format!("打开 {} 失败，请以 root 权限运行", mem_path))?;

    let mut raw_keys: Vec<(String, String)> = Vec::new();
    let mut extra_keys: Vec<String> = Vec::new();
    let mut seen_extra: std::collections::HashSet<String> = std::collections::HashSet::new();
    for (start, end) in &regions {
        scan_region(
            &mut mem_file,
            *start,
            *end,
            &salt_bytes,
            &mut raw_keys,
            &mut extra_keys,
            &mut seen_extra,
        );
    }
    eprintln!(
        "候选密钥：x'hex' {} 个，salt 邻接 {} 个",
        raw_keys.len(),
        extra_keys.len()
    );

    // 合并两类候选，统一用数据库第一页 HMAC/解密验证（match_key_hexes）。
    let mut all_key_hexes = unique_key_hexes(&raw_keys);
    for k in &extra_keys {
        if !all_key_hexes.iter().any(|x| x == k) {
            all_key_hexes.push(k.clone());
        }
    }

    let entries = if raw_keys.is_empty() && all_key_hexes.is_empty() {
        Vec::new()
    } else {
        match_key_hexes(db_dir, &all_key_hexes, &raw_keys, &db_salts)
    };

    eprintln!(
        "匹配到 {}/{} 个数据库密钥（来自 {} 个候选 key）",
        entries.len(),
        db_salts.len(),
        all_key_hexes.len()
    );
    Ok(entries)
}

#[allow(clippy::too_many_arguments)]
fn scan_region(
    mem: &mut std::fs::File,
    start: u64,
    end: u64,
    salts: &[[u8; 16]],
    results: &mut Vec<(String, String)>,
    extra_keys: &mut Vec<String>,
    seen_extra: &mut std::collections::HashSet<String>,
) {
    let total_len = (end - start) as usize;
    let overlap = MAX_PATTERN_BYTES;
    let mut offset = 0usize;

    loop {
        if offset >= total_len {
            break;
        }
        let chunk_size = std::cmp::min(CHUNK_SIZE, total_len - offset);
        let addr = start + offset as u64;

        if mem.seek(SeekFrom::Start(addr)).is_err() {
            break;
        }
        let mut buf = vec![0u8; chunk_size];
        match mem.read(&mut buf) {
            Ok(n) if n > 0 => {
                buf.truncate(n);
                search_pattern(&buf, results);
                collect_salt_adjacent_keys(&buf, salts, extra_keys, seen_extra);
            }
            _ => {}
        }

        if chunk_size > overlap {
            offset += chunk_size - overlap;
        } else {
            offset += chunk_size;
        }
    }
}

fn search_pattern(buf: &[u8], results: &mut Vec<(String, String)>) {
    scan_key_patterns(buf, results);
}
