"""
Python script to run customer finance summary sync every 1 hour using a scheduler.
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
SYNC_INTERVAL = 3600  # 1 hour in seconds

def run_sync():
    """Run the sync command"""
    manage_py = os.path.join(PROJECT_DIR, "manage.py")
    log_file = os.path.join(PROJECT_DIR, "logs", "sync_customer_finance.log")
    
    # Ensure logs directory exists
    logs_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    def log_message(msg):
        """Write message to both console and log file"""
        print(msg, flush=True)  # Force flush for pythonw
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
                f.flush()  # Force flush to file
        except Exception:
            pass  # If log write fails, continue anyway
    
    if not os.path.exists(manage_py):
        error_msg = f"[{datetime.now()}] [ERROR] Error: manage.py not found at {manage_py}"
        log_message(error_msg)
        return False
    
    # Use the same Python interpreter that's running this script
    python_cmd = sys.executable
    
    try:
        log_message(f"[{datetime.now()}] Starting sync of Customer Finance Summary...")
        log_message(f"[{datetime.now()}] Python command: {python_cmd}")
        log_message(f"[{datetime.now()}] Working directory: {PROJECT_DIR}")
        
        # Use the PC script which syncs to VPS
        pc_script = os.path.join(PROJECT_DIR, "sync_customer_finance_pc.py")
        
        if not os.path.exists(pc_script):
            error_msg = f"[{datetime.now()}] [ERROR] PC script not found at {pc_script}"
            log_message(error_msg)
            return False
        
        log_message(f"[{datetime.now()}] Running PC script: {pc_script}")
        log_message(f"[{datetime.now()}] Command: {python_cmd} sync_customer_finance_pc.py --once")
        
        result = subprocess.run(
            [python_cmd, "sync_customer_finance_pc.py", "--once"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
            env=dict(os.environ, PYTHONUNBUFFERED='1')  # Force unbuffered output
        )
        
        log_message(f"[{datetime.now()}] Subprocess completed with return code: {result.returncode}")
        
        if result.returncode == 0:
            log_message(f"[{datetime.now()}] [OK] Sync completed successfully")
            if result.stdout:
                # Write all output to log file immediately
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(result.stdout)
                        f.flush()  # Force flush
                except Exception:
                    pass
                # Also log important lines
                for line in result.stdout.split('\n'):
                    if line.strip():
                        # Log all lines, not just filtered ones
                        log_message(f"  {line.strip()}")
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
    log_file = os.path.join(PROJECT_DIR, "logs", "sync_customer_finance.log")
    logs_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    def log_message(msg):
        """Write message to both console and log file"""
        print(msg, flush=True)  # Force flush for pythonw
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
                f.flush()  # Force flush to file
        except Exception:
            pass
    
    log_message(f"Starting customer finance sync scheduler (every {SYNC_INTERVAL // 60} minutes)...")
    log_message(f"Project directory: {PROJECT_DIR}")
    log_message(f"manage.py location: {os.path.join(PROJECT_DIR, 'manage.py')}")
    log_message("Press Ctrl+C to stop")
    log_message("")  # Empty line
    
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
