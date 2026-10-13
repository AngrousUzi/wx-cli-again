/// Windows WeChat 进程内存密钥扫描器
///
/// 使用 Windows API：
/// - CreateToolhelp32Snapshot + Process32Next: 枚举进程找 Weixin.exe / WeChat.exe
/// - OpenProcess: 获取进程句柄（需要 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION）
/// - VirtualQueryEx: 枚举内存区域
/// - ReadProcessMemory: 读取内存内容
use anyhow::{bail, Result};
use std::path::Path;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::Diagnostics::Debug::ReadProcessMemory;
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32First, Process32Next, PROCESSENTRY32, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::Memory::{
    VirtualQueryEx, MEMORY_BASIC_INFORMATION, MEM_COMMIT,
};
use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ};

use super::{
    collect_db_salts, collect_masked_salt_keys, collect_salt_adjacent_keys, decode_salt_hex,
    is_writable_readable_page, match_key_hexes, scan_key_patterns, scan_masked_radium_keys,
    unique_key_hexes, KeyEntry, LINUX_RADIUM_KEY_MASK, MAX_PATTERN_BYTES,
};

const CHUNK_SIZE: usize = 2 * 1024 * 1024;

/// 查找所有 Weixin.exe / WeChat.exe 进程 PID，主进程（线程数最多）优先
fn find_wechat_pids() -> Vec<u32> {
    let snap = match unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) } {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    let mut entry = PROCESSENTRY32 {
        dwSize: std::mem::size_of::<PROCESSENTRY32>() as u32,
        ..Default::default()
    };

    let mut procs: Vec<(u32, u32)> = Vec::new();
    unsafe {
        if Process32First(snap, &mut entry).is_ok() {
            loop {
                let name = std::ffi::CStr::from_ptr(entry.szExeFile.as_ptr() as *const i8)
                    .to_string_lossy();
                if name.eq_ignore_ascii_case("Weixin.exe") || name.eq_ignore_ascii_case("WeChat.exe") {
                    procs.push((entry.th32ProcessID, entry.cntThreads));
                }
                if Process32Next(snap, &mut entry).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
    }

    // 线程数最多的主进程排在前面优先扫描
    procs.sort_by_key(|&(_, threads)| std::cmp::Reverse(threads));
    procs.into_iter().map(|(pid, _)| pid).collect()
}

#[allow(dead_code)]
pub fn scan_keys(db_dir: &Path) -> Result<Vec<KeyEntry>> {
    scan_keys_with_options(db_dir, &[])
}

pub fn scan_keys_with_options(db_dir: &Path, known: &[KeyEntry]) -> Result<Vec<KeyEntry>> {
    let pids = find_wechat_pids();
    if pids.is_empty() {
        bail!("找不到 Weixin.exe 或 WeChat.exe 进程，请确认微信正在运行");
    }

    let db_salts = collect_db_salts(db_dir);
    eprintln!("找到 {} 个加密数据库", db_salts.len());

    // salt 字节用于「salt 邻接」二进制 key 扫描：新版微信（Radium 4.x / WCDB）
    // 不再把 key 以 ASCII `x'<hex>'` 形式留在内存，只能按 DB salt 在堆上找邻接的
    // 32-byte raw key。与 macOS / Linux 扫描器同源（collect_salt_adjacent_keys）。
    let salt_bytes: Vec<[u8; 16]> = db_salts
        .iter()
        .filter_map(|(s, _)| decode_salt_hex(s))
        .collect();

    // 预计算已知 DB 的混淆 salt 模式（微信 4.x / Radium 掩码搜索）
    let masked_salts: Vec<([u8; 32], &str)> = db_salts
        .iter()
        .filter_map(|(s, _)| {
            if s.len() == 32 {
                let b = s.as_bytes();
                let mut m = [0u8; 32];
                for k in 0..32 {
                    m[k] = b[k] ^ LINUX_RADIUM_KEY_MASK[(66 + k) % 32];
                }
                Some((m, s.as_str()))
            } else {
                None
            }
        })
        .collect();

    let mut raw_keys: Vec<(String, String)> = Vec::new();
    let mut seen_masked: std::collections::HashSet<(String, String)> =
        std::collections::HashSet::new();
    let mut extra_keys: Vec<String> = Vec::new();
    let mut seen_extra: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut entries = Vec::new();

    for &pid in &pids {
        eprintln!("WeChat PID: {}", pid);

        // SAFETY: OpenProcess 请求读取权限
        let process = match unsafe {
            OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, false, pid)
        } {
            Ok(h) => h,
            Err(e) => {
                eprintln!("OpenProcess (PID: {}) 失败，跳过: {}", pid, e);
                continue;
            }
        };

        eprintln!("扫描进程内存...");
        scan_process_memory(
            process,
            &salt_bytes,
            &masked_salts,
            &mut raw_keys,
            &mut seen_masked,
            &mut extra_keys,
            &mut seen_extra,
        );

        unsafe {
            let _ = CloseHandle(process);
        }

        let mut all_key_hexes = unique_key_hexes(&raw_keys);
        for k in &extra_keys {
            if !all_key_hexes.iter().any(|x| x == k) {
                all_key_hexes.push(k.clone());
            }
        }

        if !raw_keys.is_empty() || !all_key_hexes.is_empty() {
            entries = match_key_hexes(db_dir, &all_key_hexes, &raw_keys, &db_salts);
            if !entries.is_empty() && entries.len() == db_salts.len() {
                break;
            }
        }
    }

    eprintln!(
        "候选密钥：x'hex'（含混淆）{} 个，salt 邻接 {} 个",
        raw_keys.len(),
        extra_keys.len()
    );

    let mut all_key_hexes = unique_key_hexes(&raw_keys);
    for k in &extra_keys {
        if !all_key_hexes.iter().any(|x| x == k) {
            all_key_hexes.push(k.clone());
        }
    }

    if entries.is_empty() && (!raw_keys.is_empty() || !all_key_hexes.is_empty()) {
        entries = match_key_hexes(db_dir, &all_key_hexes, &raw_keys, &db_salts);
    }

    // 把已有有效密钥并入，避免已配齐时还漏掉冷分片
    if !known.is_empty() {
        let mut by_name: std::collections::BTreeMap<String, KeyEntry> = known
            .iter()
            .cloned()
            .map(|e| (e.db_name.clone(), e))
            .collect();
        for e in entries {
            by_name.insert(e.db_name.clone(), e);
        }
        entries = by_name.into_values().collect();
    }

    eprintln!(
        "匹配到 {}/{} 个数据库密钥（来自 {} 个候选 key，含已有密钥）",
        entries.len(),
        db_salts.len(),
        all_key_hexes.len()
    );
    Ok(entries)
}

fn scan_process_memory(
    process: HANDLE,
    salts: &[[u8; 16]],
    masked_salts: &[([u8; 32], &str)],
    raw_keys: &mut Vec<(String, String)>,
    seen_masked: &mut std::collections::HashSet<(String, String)>,
    extra_keys: &mut Vec<String>,
    seen_extra: &mut std::collections::HashSet<String>,
) {
    let mut addr: usize = 0;
    const WIN_PAGE_GUARD: u32 = 0x100;

    loop {
        let mut mbi = MEMORY_BASIC_INFORMATION::default();
        // SAFETY: VirtualQueryEx 枚举进程内存区域
        let ret = unsafe {
            VirtualQueryEx(
                process,
                Some(addr as *const _),
                &mut mbi,
                std::mem::size_of::<MEMORY_BASIC_INFORMATION>(),
            )
        };
        if ret == 0 {
            break;
        }

        let region_size = mbi.RegionSize;
        let base = mbi.BaseAddress as usize;

        // 只扫描已提交的可读可写页面（含 WRITECOPY / EXECUTE_*WRITE*；见
        // `is_writable_readable_page`，从 old-main #54 捞回）。
        // 过滤 0 字节和超过 512MB 的超大映射（与 Linux / macOS 一致，防止扫描巨型虚拟内存区）
        if mbi.State == MEM_COMMIT
            && (mbi.Protect.0 & WIN_PAGE_GUARD) == 0
            && is_writable_readable_page(mbi.Protect.0)
            && region_size > 0
            && region_size < 512 * 1024 * 1024
        {
            scan_region(
                process,
                base,
                region_size,
                salts,
                masked_salts,
                raw_keys,
                seen_masked,
                extra_keys,
                seen_extra,
            );
        }

        addr = base.saturating_add(region_size);
        if addr == 0 {
            break; // overflow
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn scan_region(
    process: HANDLE,
    base: usize,
    size: usize,
    salts: &[[u8; 16]],
    masked_salts: &[([u8; 32], &str)],
    results: &mut Vec<(String, String)>,
    seen_masked: &mut std::collections::HashSet<(String, String)>,
    extra_keys: &mut Vec<String>,
    seen_extra: &mut std::collections::HashSet<String>,
) {
    let overlap = MAX_PATTERN_BYTES;
    let mut offset = 0usize;

    loop {
        if offset >= size {
            break;
        }
        let chunk_size = std::cmp::min(CHUNK_SIZE, size - offset);
        let addr = base + offset;
        let mut buf = vec![0u8; chunk_size];
        let mut bytes_read: usize = 0;

        // SAFETY: ReadProcessMemory 读取目标进程内存
        let _ = unsafe {
            ReadProcessMemory(
                process,
                addr as *const _,
                buf.as_mut_ptr() as *mut _,
                chunk_size,
                Some(&mut bytes_read),
            )
        };

        if bytes_read > 0 {
            buf.truncate(bytes_read);
            // 1) 传统明文 ASCII 模式 `x'<key><salt>'`
            search_pattern(&buf, results);
            // 2) 微信 4.x (Radium / WCDB) 掩码混淆 key 模式
            scan_masked_radium_keys(&buf, &LINUX_RADIUM_KEY_MASK, results, seen_masked);
            collect_masked_salt_keys(
                &buf,
                masked_salts,
                &LINUX_RADIUM_KEY_MASK,
                results,
                seen_masked,
            );
            // 3) salt 邻接 raw binary key 模式
            collect_salt_adjacent_keys(&buf, salts, extra_keys, seen_extra);
        }

        if bytes_read == 0 {
            offset += chunk_size;
        } else if bytes_read < chunk_size {
            offset += bytes_read;
        } else if chunk_size > overlap {
            offset += chunk_size - overlap;
        } else {
            offset += chunk_size;
        }
    }
}

fn search_pattern(buf: &[u8], results: &mut Vec<(String, String)>) {
    scan_key_patterns(buf, results);
}
