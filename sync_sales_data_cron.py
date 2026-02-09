"""
Python script to run sync every 7 minutes using a scheduler.
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
SYNC_INTERVAL = 420  # 7 minutes in seconds

def run_sync():
    """Run the sync command"""
    manage_py = os.path.join(PROJECT_DIR, "manage.py")
    
    if not os.path.exists(manage_py):
        print(f"[{datetime.now()}] ✗ Error: manage.py not found at {manage_py}")
        return False
    
    # Use the same Python interpreter that's running this script
    python_cmd = sys.executable
    
    try:
        print(f"[{datetime.now()}] Starting sync of AR Invoices and Credit Memos...")
        
        result = subprocess.run(
            [python_cmd, "manage.py", "sync_all_sales_data"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            print(f"[{datetime.now()}] ✓ Sync completed successfully")
            if result.stdout:
                # Print only important lines from output
                for line in result.stdout.split('\n'):
                    if line.strip() and ('✓' in line or '✗' in line or 'Error' in line or 'Success' in line):
                        print(f"  {line}")
            return True
        else:
            print(f"[{datetime.now()}] ✗ Sync failed with return code {result.returncode}")
            if result.stderr:
                print("Error output:", result.stderr)
            if result.stdout:
                print("Output:", result.stdout)
            return False
                
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now()}] ✗ Sync timed out after 10 minutes")
        return False
    except Exception as e:
        print(f"[{datetime.now()}] ✗ Error running sync: {str(e)}")
        return False

def main():
    """Main loop"""
    print(f"Starting sync scheduler (every {SYNC_INTERVAL // 60} minutes)...")
    print(f"Project directory: {PROJECT_DIR}")
    print(f"manage.py location: {os.path.join(PROJECT_DIR, 'manage.py')}")
    print("Press Ctrl+C to stop\n")
    
    try:
        while True:
            run_sync()
            print(f"[{datetime.now()}] Waiting {SYNC_INTERVAL // 60} minutes before next sync...\n")
            time.sleep(SYNC_INTERVAL)
    except KeyboardInterrupt:
        print("\nSync scheduler stopped by user")

if __name__ == "__main__":
    main()
