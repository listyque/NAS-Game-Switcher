/* Keep the Python/Qt runtime out of the portable folder's root. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static BOOL overlays_disabled(const WCHAR *root) {
    WCHAR path[32768];
    char buf[8192];
    HANDLE file;
    DWORD read_bytes = 0;
    if (swprintf_s(path, 32768, L"%lsdata\\config\\settings.json", root) < 0) return TRUE;
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return TRUE;
    if (ReadFile(file, buf, sizeof(buf) - 1, &read_bytes, NULL) && read_bytes > 0) {
        buf[read_bytes] = 0;
        for (DWORD i = 0; i + 30 < read_bytes; ++i) {
            if (memcmp(buf + i, "\"disable_performance_overlays\"", 30) == 0) {
                const char *p = buf + i + 30;
                while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n' || *p == ':') ++p;
                CloseHandle(file);
                return strncmp(p, "false", 5) != 0;
            }
        }
    }
    CloseHandle(file);
    return TRUE;
}

static void record_exit(const WCHAR *root, DWORD pid, DWORD status, ULONGLONG started) {
    WCHAR path[32768];
    char line[256];
    HANDLE file;
    BY_HANDLE_FILE_INFORMATION info;
    LARGE_INTEGER size, zero = {0};
    SYSTEMTIME now;
    DWORD written, attributes;
    int length;
    const WCHAR *folders[] = {L"data", L"data\\logs"};
    for (int i = 0; i < 2; ++i) {
        swprintf_s(path, 32768, L"%ls%ls", root, folders[i]);
        attributes = GetFileAttributesW(path);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)
                || attributes & FILE_ATTRIBUTE_REPARSE_POINT) return;
    }
    swprintf_s(path, 32768, L"%lsdata\\logs\\process-exit.jsonl", root);
    file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    if (!GetFileInformationByHandle(file, &info) || info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT
            || !GetFileSizeEx(file, &size)) goto done;
    if (size.QuadPart >= 65536) {
        if (!SetFilePointerEx(file, zero, NULL, FILE_BEGIN) || !SetEndOfFile(file)) goto done;
    } else if (!SetFilePointerEx(file, zero, NULL, FILE_END)) goto done;
    GetSystemTime(&now);
    length = sprintf_s(line, sizeof(line),
        "{\"time_utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"pid\":%lu,\"exit_code\":%lu,\"exit_hex\":\"0x%08lX\",\"elapsed_ms\":%llu}\n",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond,
        pid, status, status, GetTickCount64() - started);
    // The parent survives native runtime exits, including exits with no Windows fault event.
    if (length > 0 && WriteFile(file, line, (DWORD)length, &written, NULL)) FlushFileBuffers(file);
done:
    CloseHandle(file);
}

static BOOL start_recovery(const WCHAR *root, BOOL no_restart) {
    WCHAR system[32768], executable[32768], command[32768], cache[32768];
    STARTUPINFOW startup = {sizeof(startup)};
    PROCESS_INFORMATION process = {0};
    if (!GetSystemDirectoryW(system, 32768) || wcslen(system) + 64 >= 32768) return FALSE;
    swprintf_s(executable, 32768, L"%ls\\WindowsPowerShell\\v1.0\\powershell.exe", system);
    swprintf_s(cache, 32768, L"%lsdata\\cache\\powershell\\ModuleAnalysisCache", root);
    SetEnvironmentVariableW(L"PSModuleAnalysisCachePath", cache);
    if (wcslen(executable) + 3 * wcslen(root) + 256 >= 32768) return FALSE;
    swprintf_s(command, 32768,
        L"\"%ls\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%lsdata\\updates\\apply.ps1\" -Root \"%ls.\" -Recover -ParentPid %lu%ls",
        executable, root, root, GetCurrentProcessId(), no_restart ? L" -NoRestart" : L"");
    /* Recovery waits for this bootstrap to release NGS.exe before restoring it. */
    if (!CreateProcessW(executable, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                        NULL, NULL, &startup, &process)) return FALSE;
    AllowSetForegroundWindow(process.dwProcessId);
    CloseHandle(process.hThread); CloseHandle(process.hProcess);
    return TRUE;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    WCHAR executable[32768], command[32768], root[32768], journal[32768];
    WCHAR close_event_name[128];
    DWORD length = GetModuleFileNameW(NULL, executable, 32768);
    WCHAR *separator;
    STARTUPINFOW startup = {sizeof(startup)};
    PROCESS_INFORMATION process = {0};
    DWORD status = 1;
    ULONGLONG started = GetTickCount64();
    HANDLE orderly_close = NULL;
    BOOL deliberately_closed = FALSE;
    BOOL root_known = FALSE;
    BOOL diagnostic_check = wcsstr(arguments, L"--self-test") != NULL
                         || wcsstr(arguments, L"--update-probe") != NULL;
    if (!length || length >= 32768 || !(separator = wcsrchr(executable, L'\\')))
        goto failure;
    separator[1] = 0;
    wcscpy_s(root, 32768, executable);
    if (wcslen(root) + 64 >= 32768) goto failure;
    root_known = TRUE;
    swprintf_s(journal, 32768, L"%lsdata\\updates\\transaction.json", root);
    if (GetFileAttributesW(journal) != INVALID_FILE_ATTRIBUTES) {
        if (!start_recovery(root, FALSE)) goto failure;
        return 0;
    }
    /* An in-memory event belongs to this launch only. It is never an update
       health acknowledgement and cannot survive as a stale disk marker. */
    SetEnvironmentVariableW(L"NGS_FIRST_START_CLOSE_EVENT", NULL);
    swprintf_s(journal, 32768, L"%lsdata\\updates\\first-start.json", root);
    if (!diagnostic_check && GetFileAttributesW(journal) != INVALID_FILE_ATTRIBUTES) {
        swprintf_s(close_event_name, 128, L"Local\\NGS.FirstStart.%lu.%llu",
                   GetCurrentProcessId(), started);
        SetLastError(ERROR_SUCCESS);
        orderly_close = CreateEventW(NULL, TRUE, FALSE, close_event_name);
        if (!orderly_close || GetLastError() == ERROR_ALREADY_EXISTS
                || !SetEnvironmentVariableW(L"NGS_FIRST_START_CLOSE_EVENT", close_event_name))
            goto failure;
    }
    if (wcslen(executable) + wcslen(L"runtime\\NGS.exe") >= 32768)
        goto failure;
    wcscat_s(executable, 32768, L"runtime\\NGS.exe");
    if (wcslen(executable) + wcslen(arguments) + 4 >= 32768)
        goto failure;
restart_runtime:
    started = GetTickCount64();
    swprintf_s(command, 32768, L"\"%ls\" %ls", executable, arguments);
    /* An explicit executable path avoids PATH/CWD lookup; no shell interprets arguments. */
    {
        BOOL launched = FALSE;
        if (overlays_disabled(root)) {
            SIZE_T attr_size = 0;
            InitializeProcThreadAttributeList(NULL, 1, 0, &attr_size);
            if (attr_size > 0 && attr_size <= 256) {
                BYTE attr_buf[256];
                LPPROC_THREAD_ATTRIBUTE_LIST attr_list = (LPPROC_THREAD_ATTRIBUTE_LIST)attr_buf;
                DWORD64 mitigation = 0x00000001ULL << 32; /* EXTENSION_POINT_DISABLE_ALWAYS_ON */
                STARTUPINFOEXW startup_ex = {0};
                startup_ex.StartupInfo.cb = sizeof(startup_ex);
                if (InitializeProcThreadAttributeList(attr_list, 1, 0, &attr_size)) {
                    if (UpdateProcThreadAttribute(attr_list, 0, 0x00020007 /* PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY */,
                                                  &mitigation, sizeof(mitigation), NULL, NULL)) {
                        startup_ex.lpAttributeList = attr_list;
                        launched = CreateProcessW(executable, command, NULL, NULL, FALSE,
                                                  EXTENDED_STARTUPINFO_PRESENT, NULL, NULL,
                                                  &startup_ex.StartupInfo, &process);
                    }
                    DeleteProcThreadAttributeList(attr_list);
                }
            }
        }
        if (!launched && !CreateProcessW(executable, command, NULL, NULL, FALSE, 0, NULL, NULL, &startup, &process))
            goto failure;
    }
    SetEnvironmentVariableW(L"NGS_FIRST_START_CLOSE_EVENT", NULL);
    AllowSetForegroundWindow(process.dwProcessId);
    CloseHandle(process.hThread);
    if (WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0
            && GetExitCodeProcess(process.hProcess, &status))
        record_exit(root, process.dwProcessId, status, started);
    CloseHandle(process.hProcess);
    if (status == 75) goto restart_runtime;
    deliberately_closed = status == 0 && orderly_close
                       && WaitForSingleObject(orderly_close, 0) == WAIT_OBJECT_0;
    if (orderly_close) { CloseHandle(orderly_close); orderly_close = NULL; }
    swprintf_s(journal, 32768, L"%lsdata\\updates\\first-start.json", root);
    if (!diagnostic_check && !deliberately_closed
            && GetFileAttributesW(journal) != INVALID_FILE_ATTRIBUTES) {
        /* No timer/polling: an exit before the GUI acknowledgement uses the
           existing process wait. An explicit, clean early close keeps pending
           and backup for the next manual launch instead of rolling it back. */
        if (!start_recovery(root, status == 0)) goto failure;
    }
    return (int)status;
failure:
    SetEnvironmentVariableW(L"NGS_FIRST_START_CLOSE_EVENT", NULL);
    if (orderly_close) CloseHandle(orderly_close);
    if (root_known && !diagnostic_check) {
        swprintf_s(journal, 32768, L"%lsdata\\updates\\first-start.json", root);
        if (GetFileAttributesW(journal) != INVALID_FILE_ATTRIBUTES && start_recovery(root, FALSE))
            return 1;
    }
    MessageBoxW(NULL, L"Could not start NGS. Extract the whole folder, including runtime, and try again.",
                L"NAS Game Switcher", MB_OK | MB_ICONERROR);
    return 1;
}
