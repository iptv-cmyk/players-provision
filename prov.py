import os
import sys
import socket
import subprocess
import concurrent.futures
import urllib.request
import zipfile
import platform
import time

# ==================== CONFIGURATION ====================
APK_PATH = r"app-release.apk"         # Assumes APK is in the same folder as the script
PACKAGE_NAME = "tech.vvs.vvs_launcher"
MAIN_ACTIVITY = ".MainActivity"       
ADMIN_RECEIVER = ".AdminReceiver"
TARGET_PORT = 5555                    # Standard ADB port
SCAN_TIMEOUT_SECONDS = 2.0            # Increased timeout to prevent missing slower/Wi-Fi devices
MAX_SCAN_THREADS = 100                # Increased thread count for rapid sweeping
MAX_DEPLOY_THREADS = 8                # Moderate thread count for heavy APK pushing
ADB_TIMEOUT_SECONDS = 12              
FORCE_REINSTALL = False               # Skip install if app already exists (prevents Play Protect timeouts)
# =======================================================

ADB_CMD = "adb" 

def get_local_subnet_prefix():
    """Detects the local IP address and returns the /24 subnet prefix."""
    try:
        # Connect to an external dummy socket to find the true active local interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        
        parts = local_ip.split(".")
        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        print(f"[+] Detected Host IP: {local_ip} -> Targeting Subnet Range: {prefix}.1 to {prefix}.254")
        return prefix
    except Exception:
        print("[!] Could not auto-detect local network interface. Defaulting to 192.168.1")
        return "192.168.1"

def check_ip_port(ip):
    """Checks if port 5555 is open on a given IP address with a retry."""
    # Attempt 1
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(SCAN_TIMEOUT_SECONDS)
            result = s.connect_ex((ip, TARGET_PORT))
            if result == 0:
                return ip
    except Exception:
        pass
        
    # Attempt 2 (Retry to tolerate packet loss/latency jitter)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(SCAN_TIMEOUT_SECONDS)
            result = s.connect_ex((ip, TARGET_PORT))
            if result == 0:
                return ip
    except Exception:
        pass
        
    return None

def discover_tv_players():
    """Sweeps the /24 network concurrently looking for open ADB ports."""
    prefix = get_local_subnet_prefix()
    print(f"Scanning network range on port {TARGET_PORT}...")
    
    discovered_ips = []
    ip_pool = [f"{prefix}.{i}" for i in range(1, 255)]
    
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SCAN_THREADS) as executor:
        for ip in ip_pool:
            futures.append(executor.submit(check_ip_port, ip))
            time.sleep(0.01)  # 10ms stagger to prevent packet drops and SYN-flood limits
            
        for future in concurrent.futures.as_completed(futures):
            ip = future.result()
            if ip:
                discovered_ips.append(ip)
                print(f"  [Found] Active Android Player detected at {ip}")
                
    # Sort IPs numerically for clean final report
    discovered_ips.sort(key=lambda x: list(map(int, x.split('.'))))
    print(f"[+] Scan Complete. Found {len(discovered_ips)} active player(s).\n")
    return discovered_ips

def bootstrap_environment():
    """Ensures all prerequisites (ADB, files) exist before starting."""
    global ADB_CMD
    print("Running system prerequisite checks...")

    if not os.path.exists(APK_PATH):
        print(f"[!] Critical Error: Target APK not found at: {APK_PATH}")
        sys.exit(1)

    try:
        subprocess.run(["adb", "version"], capture_output=True, check=True)
        print("[+] System ADB found.")
        ADB_CMD = "adb"
    except (subprocess.CalledProcessError, FileNotFoundError):
        system_os = platform.system()
        adb_filename = "adb.exe" if system_os == "Windows" else "adb"
        local_adb_path = os.path.join(os.getcwd(), "platform-tools", adb_filename)

        if not os.path.exists(local_adb_path):
            print(f"[-] Local ADB missing. Downloading official Android platform-tools for {system_os}...")
            download_and_extract_adb()

        if os.path.exists(local_adb_path):
            print("[+] Local ADB found.")
            # Set executable permissions on non-Windows systems
            if system_os != "Windows":
                try:
                    os.chmod(local_adb_path, 0o755)
                    print("[+] Set executable permission on local ADB.")
                except Exception as e:
                    print(f"[!] Failed to set executable permission on local adb: {e}")
            ADB_CMD = local_adb_path
        else:
            print(f"[!] Critical Error: Local ADB path does not exist after download: {local_adb_path}")
            sys.exit(1)

def download_and_extract_adb():
    """Downloads and extracts Google's official platform-tools for the host OS."""
    system_os = platform.system()
    if system_os == "Windows":
        url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
    elif system_os == "Linux":
        url = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"
    elif system_os == "Darwin":
        url = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
    else:
        print(f"[!] Unsupported operating system: {system_os}")
        sys.exit(1)

    zip_path = "platform-tools.zip"
    try:
        print(f"Downloading ADB for {system_os} from {url}...")
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(os.getcwd())
        os.remove(zip_path)
        print("[+] ADB dependency downloaded successfully.")
    except Exception as e:
        print(f"[!] Failed to download ADB: {e}")
        sys.exit(1)

def execute_adb_command(command, timeout=ADB_TIMEOUT_SECONDS):
    try:
        # command can be either a list or a string. If it's a list, we do not use shell=True.
        # If it's a string, we run it using shell=True.
        is_shell = isinstance(command, str)
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, shell=is_shell)
        return process.stdout, process.stderr
    except subprocess.TimeoutExpired:
        return "", "EXECUTION_TIMEOUT"

def get_apk_version_code(apk_path):
    """Parses the AndroidManifest.xml binary inside the APK without external dependencies to get versionCode."""
    import zipfile, struct
    try:
        with zipfile.ZipFile(apk_path) as z:
            data = z.read("AndroidManifest.xml")
        magic, size = struct.unpack("<II", data[0:8])
        if magic != 0x00080003:
            return None
        chunk_type, chunk_size = struct.unpack("<II", data[8:16])
        if chunk_type != 0x001c0001:
            return None
        string_pool = data[8:8+chunk_size]
        num_strings = struct.unpack("<I", string_pool[8:12])[0]
        flags = struct.unpack("<I", string_pool[16:20])[0]
        strings_offset = struct.unpack("<I", string_pool[20:24])[0]
        offsets = struct.unpack(f"<{num_strings}I", string_pool[28:28+num_strings*4])
        
        strings = []
        is_utf8 = bool(flags & 0x00000100)
        for offset in offsets:
            str_start = strings_offset + offset
            if is_utf8:
                idx = str_start
                val = string_pool[idx]
                idx += 2 if val & 0x80 else 1
                val = string_pool[idx]
                idx += 2 if val & 0x80 else 1
                str_end = string_pool.find(b"\x00", idx)
                if str_end == -1:
                    str_end = idx
                s = string_pool[idx:str_end].decode("utf-8", errors="ignore")
            else:
                length = struct.unpack("<H", string_pool[str_start:str_start+2])[0]
                if length & 0x8000:
                    length = ((length & 0x7FFF) << 16) | struct.unpack("<H", string_pool[str_start+2:str_start+4])[0]
                    str_start += 4
                else:
                    str_start += 2
                s = string_pool[str_start:str_start+length*2].decode("utf-16le", errors="ignore")
            strings.append(s)
            
        try:
            version_code_idx = strings.index("versionCode")
        except ValueError:
            return None
            
        offset = 8 + chunk_size
        while offset < len(data):
            if offset + 8 > len(data):
                break
            ctype, csize = struct.unpack("<II", data[offset:offset+8])
            if ctype == 0x00100102: # START_TAG
                if offset + 36 <= len(data):
                    attr_start, attr_size, attr_count = struct.unpack("<HHH", data[offset+24:offset+30])
                    attr_offset = offset + 16 + attr_start
                    for _ in range(attr_count):
                        if attr_offset + 20 <= len(data):
                            ns, name, raw_val, val_type, val_data = struct.unpack("<5I", data[attr_offset:attr_offset+20])
                            if name == version_code_idx:
                                return val_data
                            attr_offset += 20
            offset += csize
    except Exception:
        pass
    return None

def deploy_to_device(ip):
    """Manages the installation lifecycle for a single TV."""
    target_socket = f"{ip}:5555"
    
    # 1. Connect over LAN
    stdout, stderr = execute_adb_command([ADB_CMD, "connect", target_socket])
    if "connected" not in stdout and "already connected" not in stdout:
        err_details = stderr.strip() if stderr else stdout.strip()
        return ip, f"CONNECTION_FAILED ({err_details})"
        
    # 2. Check if the app is already installed
    pkg_path_stdout, pkg_path_stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "pm", "path", PACKAGE_NAME])
    is_installed = "package:" in pkg_path_stdout
    
    # Get local and installed version codes
    local_version_code = get_apk_version_code(APK_PATH)
    installed_ver_code = None
    if is_installed:
        pkg_info_stdout, pkg_info_stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "dumpsys", "package", PACKAGE_NAME])
        for line in pkg_info_stdout.splitlines():
            if "versionCode=" in line:
                parts = line.strip().split()
                for part in parts:
                    if part.startswith("versionCode="):
                        try:
                            installed_ver_code = int(part.split("=")[1])
                        except ValueError:
                            pass
                break
                
    # Compare version codes to determine if upgrade is needed
    needs_install = True
    if is_installed:
        if local_version_code is not None and installed_ver_code is not None:
            if local_version_code <= installed_ver_code:
                needs_install = False
        else:
            # Fallback to skip if we fail to parse version codes
            needs_install = False
            
    if FORCE_REINSTALL:
        needs_install = True
        
    install_success = False
    status = "SUCCESS"
    
    if is_installed and not needs_install:
        ver_str = f" (v{installed_ver_code} is up-to-date)" if installed_ver_code else ""
        print(f"  [Device {ip}] App is already installed{ver_str}. Skipping installation.")
        install_success = True
        status = "ALREADY_INSTALLED"
    else:
        if is_installed:
            ver_str = f" (v{installed_ver_code} -> v{local_version_code})" if installed_ver_code and local_version_code else ""
            print(f"  [Device {ip}] Upgrading app{ver_str}...")
        else:
            ver_str = f" (v{local_version_code})" if local_version_code else ""
            print(f"  [Device {ip}] App not found. Installing APK{ver_str}...")
            
        # 3. Push APK to temporary local storage to prevent streaming socket hangs on older or slow network devices
        print(f"  [Device {ip}] Uploading APK to target (this may take a moment on slow Wi-Fi)...")
        temp_apk_path = "/data/local/tmp/app.apk"
        push_stdout, push_stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "push", APK_PATH, temp_apk_path], timeout=90)
        
        if "pushed" in push_stdout or "pushed" in push_stderr or "1 file pushed" in push_stdout or "1 file pushed" in push_stderr:
            print(f"  [Device {ip}] Upload complete. Executing local system installation...")
            # Reinstall (-r), allow test APKs (-t), and auto-grant permissions (-g)
            inst_stdout, inst_stderr = execute_adb_command([
                ADB_CMD, "-s", target_socket, "shell", "pm", "install", "-r", "-t", "-g", temp_apk_path
            ], timeout=60)
            
            # Clean up temp file
            execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "rm", temp_apk_path])
            
            if "Success" in inst_stdout:
                install_success = True
                status = "SUCCESS" if not is_installed else "UPGRADED"
            else:
                error_details = inst_stderr.strip() if inst_stderr else inst_stdout.strip()
                if error_details == "EXECUTION_TIMEOUT":
                    error_details = "Local system installation timed out"
                install_success = False
                status = f"INSTALL_FAILED ({error_details})"
        else:
            error_details = push_stderr.strip() if push_stderr else push_stdout.strip()
            if error_details == "EXECUTION_TIMEOUT":
                error_details = "APK upload timed out (slow connection or Wi-Fi packet drops)"
            install_success = False
            status = f"UPLOAD_FAILED ({error_details})"
            
    if install_success:
        # 4. Configure App as Device Owner
        admin_target = f"{PACKAGE_NAME}/{ADMIN_RECEIVER}"
        execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "settings", "put", "global", "device_provisioned", "0"])
        execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "settings", "put", "secure", "user_setup_complete", "0"])
        
        dpm_stdout, dpm_stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "dpm", "set-device-owner", admin_target])
        
        execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "settings", "put", "global", "device_provisioned", "1"])
        execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "settings", "put", "secure", "user_setup_complete", "1"])
        
        # Check if device owner is active (either successfully set or already set)
        is_owner = ("Success" in dpm_stdout or 
                    "Active admin set" in dpm_stdout or 
                    "already set" in dpm_stdout or 
                    "already set" in dpm_stderr)
        
        owner_status = "YES" if is_owner else "NO"
        
        # Log device owner result details
        if is_owner:
            print(f"  [Device {ip}] Device owner successfully configured.")
        else:
            dpm_err = dpm_stderr.strip() if dpm_stderr else dpm_stdout.strip()
            print(f"  [Device {ip}] Note/Warning on Device Owner setup: {dpm_err}")

        # 5. Force launch application activity into foreground
        launch_target = f"{PACKAGE_NAME}/{MAIN_ACTIVITY}"
        execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "am", "start", "-n", launch_target])
        
        # Set final detailed status
        if status == "SUCCESS":
            status = f"SUCCESS (Device Owner: {owner_status})"
        elif status == "UPGRADED":
            status = f"UPGRADED (Device Owner: {owner_status})"
        elif status == "ALREADY_INSTALLED":
            status = f"ALREADY_INSTALLED (Device Owner: {owner_status})"
        
    # 6. Tear down network socket
    execute_adb_command([ADB_CMD, "disconnect", target_socket])
    return ip, status

def main():
    bootstrap_environment()
    
    # Handle command-line arguments for specific actions
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("-h", "--help"):
            print("Android TV Provisioning Tool")
            print("Usage:")
            print("  python prov.py                       # Provision all boxes on the local subnet")
            print("  python prov.py -u <IP>               # Uninstall app from a specific IP")
            print("  python prov.py --uninstall <IP>      # Uninstall app from a specific IP")
            sys.exit(0)
            
        elif arg in ("-u", "--uninstall") and len(sys.argv) > 2:
            target_ip = sys.argv[2]
            print(f"\n========================================")
            print(f"    UNINSTALLING APP FROM SPECIFIC IP   ")
            print(f"========================================")
            print(f"Target Device: {target_ip}\n")
            
            target_socket = f"{target_ip}:5555"
            execute_adb_command([ADB_CMD, "start-server"])
            
            # 1. Connect
            print(f"[*] Connecting to {target_socket}...")
            stdout, stderr = execute_adb_command([ADB_CMD, "connect", target_socket])
            if "connected" not in stdout and "already connected" not in stdout:
                err_details = stderr.strip() if stderr else stdout.strip()
                print(f"[!] Connection failed: {err_details}")
                sys.exit(1)
                
            # 2. Trigger programmatic clearing backdoor via ADB Broadcast (if updated app is present)
            uninstall_receiver_target = f"{PACKAGE_NAME}/.UninstallReceiver"
            admin_target = f"{PACKAGE_NAME}/{ADMIN_RECEIVER}"
            print(f"[*] Triggering programmatic Device Owner clearing broadcast...")
            execute_adb_command([
                ADB_CMD, "-s", target_socket, "shell", "am", "broadcast",
                "-a", f"{PACKAGE_NAME}.CLEAR_DEVICE_OWNER",
                "-n", uninstall_receiver_target,
                "-f", "32"
            ])
            
            # 3. Remove active device admin/owner
            print(f"[*] Removing active device admin: {admin_target}...")
            dpm_stdout, dpm_stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "shell", "dpm", "remove-active-admin", admin_target])
            if dpm_stderr:
                print(f"    Note: {dpm_stderr.strip()}")
                
            # 4. Uninstall package
            print(f"[*] Uninstalling package: {PACKAGE_NAME}...")
            stdout, stderr = execute_adb_command([ADB_CMD, "-s", target_socket, "uninstall", PACKAGE_NAME])
            
            if "Success" in stdout or "Success" in stderr:
                print(f"\n[+] SUCCESS: Fully uninstalled {PACKAGE_NAME} from {target_ip}.")
            else:
                err_details = stderr.strip() if stderr else stdout.strip()
                print(f"\n[!] WARNING: Uninstall status: {err_details}")
                
                # Check for standard Device Owner non-test admin restriction
                if "DELETE_FAILED_INTERNAL_ERROR" in err_details:
                    print("\n" + "="*60)
                    print("🛡️  ANDROID SECURITY CONSTRAINT: DEVICE OWNER PROTECTION DETECTED")
                    print("="*60)
                    print("The TV Box is protecting this app from uninstallation because it is the registered Device Owner.\n")
                    print("To cleanly uninstall this app WITHOUT factory resetting your TV:")
                    print("1. Re-compile and deploy the updated APK (with the new AdminReceiver backdoor) using: python prov.py")
                    print("2. Re-run this uninstall command: python prov.py -u " + target_ip)
                    print("="*60 + "\n")
                
            # 4. Disconnect
            execute_adb_command([ADB_CMD, "disconnect", target_socket])
            sys.exit(0)

    # Default flow: Run the network socket scanner
    tv_ips = discover_tv_players()
        
    if not tv_ips:
        print("[!] No active Android TV devices responded on port 5555. Aborting deployment.")
        sys.exit(0)
        
    print(f"Initializing fleet upgrade across {len(tv_ips)} discovered nodes...\n")
    execute_adb_command([ADB_CMD, "start-server"])
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DEPLOY_THREADS) as executor:
        future_to_ip = {executor.submit(deploy_to_device, ip): ip for ip in tv_ips}
        
        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                device_ip, device_status = future.result()
                results[device_ip] = device_status
                print(f"[Device {device_ip}] -> {device_status}")
            except Exception as exc:
                results[ip] = f"THREAD_EXC ({exc})"
                print(f"[Device {ip}] -> THREAD ERROR")

    print("\n" + "="*40)
    print("         DEPLOYMENT FINAL SUMMARY")
    print("="*40)
    for ip, status in results.items():
        print(f"Room Target: {ip:<18} Status: {status}")

if __name__ == "__main__":
    main()
