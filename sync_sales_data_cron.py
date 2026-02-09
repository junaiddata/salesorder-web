"""
Python script to run sync every 5 minutes using a scheduler.
This can be run as a standalone script or as a Windows service.
Place this file in the same directory as manage.py
"""
import time
import subprocess
import sys
import os
from datetime import datetime

# Get the directory where this script is located (should be same as manage.py)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_INTERVAL = 300  # 5 minutes in seconds

def run_sync():
    """Run the sync command"""
    manage_py = os.path.join(PROJECT_DIR, "manage.py")
    log_file = os.path.join(PROJECT_DIR, "logs", "sync_sales_data.log")
    
    # Ensure logs directory exists
    logs_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    def log_message(msg):
        """Write message to both console and log file"""
        print(msg)
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
        except Exception:
            pass  # If log write fails, continue anyway
    
    if not os.path.exists(manage_py):
        error_msg = f"[{datetime.now()}] [ERROR] Error: manage.py not found at {manage_py}"
        log_message(error_msg)
        return False
    
    # Use the same Python interpreter that's running this script
    python_cmd = sys.executable
    
    try:
        log_message(f"[{datetime.now()}] Starting sync of AR Invoices and Credit Memos...")
        
        result = subprocess.run(
            [python_cmd, "manage.py", "sync_all_sales_data"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            log_message(f"[{datetime.now()}] [OK] Sync completed successfully")
            if result.stdout:
                # Write all output to log file
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(result.stdout)
                except Exception:
                    pass
                # Print only important lines to console
                for line in result.stdout.split('\n'):
                    if line.strip() and ('[OK]' in line or '[ERROR]' in line or 'Error' in line or 'Success' in line or 'SYNC' in line):
                        # Replace Unicode characters with ASCII-safe alternatives
                        safe_line = line.encode('ascii', 'replace').decode('ascii')
                        print(f"  {safe_line}")
            return True
        else:
            error_msg = f"[{datetime.now()}] [ERROR] Sync failed with return code {result.returncode}"
            log_message(error_msg)
            if result.stderr:
                log_message(f"Error output: {result.stderr}")
            if result.stdout:
                log_message(f"Output: {result.stdout}")
            return False
                
    except subprocess.TimeoutExpired:
        error_msg = f"[{datetime.now()}] [ERROR] Sync timed out after 10 minutes"
        log_message(error_msg)
        return False
    except Exception as e:
        error_msg = f"[{datetime.now()}] [ERROR] Error running sync: {str(e)}"
        log_message(error_msg)
        return False

def main():
    """Main loop"""
    print(f"Starting sync scheduler (every {SYNC_INTERVAL // 60} minutes)...")
    print(f"Project directory: {PROJECT_DIR}")
    print(f"manage.py location: {os.path.join(PROJECT_DIR, 'manage.py')}")
    print("Press Ctrl+C to stop\n")
    
    log_file = os.path.join(PROJECT_DIR, "logs", "sync_sales_data.log")
    logs_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    def log_message(msg):
        """Write message to both console and log file"""
        print(msg)
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
        except Exception:
            pass
    
    try:
        while True:
            run_sync()
            wait_msg = f"[{datetime.now()}] Waiting {SYNC_INTERVAL // 60} minutes before next sync..."
            log_message(wait_msg)
            log_message("")  # Empty line
            time.sleep(SYNC_INTERVAL)
    except KeyboardInterrupt:
        stop_msg = "\nSync scheduler stopped by user"
        log_message(stop_msg)

if __name__ == "__main__":
    main()
