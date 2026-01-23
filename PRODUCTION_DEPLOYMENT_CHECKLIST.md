# Production Deployment Checklist

## 🚀 Pre-Deployment Checklist

### 1. **Settings Configuration** (`salesorder/settings.py`)

#### Security Settings (CRITICAL)
- [ ] **Change `SECRET_KEY`**: Generate a new secret key for production
  ```python
  # Generate new key: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
  SECRET_KEY = os.getenv('SECRET_KEY', 'your-new-secret-key-here')
  ```

- [ ] **Set `DEBUG = False`**: 
  ```python
  DEBUG = os.getenv('DEBUG', 'False') == 'True'  # Only True in development
  ```

- [ ] **Update `ALLOWED_HOSTS`**: 
  ```python
  ALLOWED_HOSTS = ['salesorder.junaidworld.com', 'www.salesorder.junaidworld.com']
  # Or use environment variable:
  ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '').split(',') if os.getenv('ALLOWED_HOSTS') else []
  ```

- [ ] **Restrict CORS**: 
  ```python
  CORS_ALLOW_ALL_ORIGINS = False  # Change from True
  CORS_ALLOWED_ORIGINS = [
      "https://salesorder.junaidworld.com",
      # Add other allowed origins
  ]
  ```

#### API Configuration
- [ ] **VPS_BASE_URL** (in `settings.py`):
  ```python
  VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
  ```

- [ ] **VPS_API_KEY** (MUST MATCH between PC script and VPS):
  ```python
  # Use environment variable for security
  VPS_API_KEY = os.getenv('VPS_API_KEY', 'your-secure-random-key-here')
  ```
  **⚠️ IMPORTANT**: This key must be the SAME in:
  - `salesorder/settings.py` (VPS side)
  - `salesorder/so/management/commands/sync_salesorders_api.py` (PC side)

- [ ] **SAP_API_BASE_URL** (only used on PC, not needed on VPS):
  ```python
  # This is only used on your PC, not on VPS
  SAP_API_BASE_URL = "http://192.168.1.103/IntegrationApi/api/SalesOrder"
  ```

### 2. **Environment Variables** (`.env` file on VPS)

Create a `.env` file in the project root with:
```bash
# Security
SECRET_KEY=your-generated-secret-key-here
DEBUG=False

# Database (if using PostgreSQL/MySQL)
DB_ENGINE=django.db.backends.postgresql
DB_NAME=salesorder_db
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=localhost
DB_PORT=5432

# VPS API Configuration
VPS_BASE_URL=https://salesorder.junaidworld.com
VPS_API_KEY=your-secure-random-key-here-must-match-pc-script

# Allowed Hosts (comma-separated)
ALLOWED_HOSTS=salesorder.junaidworld.com,www.salesorder.junaidworld.com
```

### 3. **Database Migrations**

- [ ] **Run migrations on VPS**:
  ```bash
  cd salesorder
  python manage.py makemigrations
  python manage.py migrate
  ```

- [ ] **Verify new fields exist**:
  - `SAPSalesorder.last_synced_at` (if migration was created)
  - Database indexes (should be created automatically)

### 4. **PC Script Configuration** (`sync_salesorders_api.py`)

On your **PC** (where you run the sync command):

- [ ] **Update `VPS_BASE_URL`** in `sync_salesorders_api.py`:
  ```python
  VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
  ```

- [ ] **Update `VPS_API_KEY`** (MUST match VPS):
  ```python
  VPS_API_KEY = os.getenv('VPS_API_KEY', 'your-secure-random-key-here-must-match-vps')
  ```

- [ ] **Set environment variables on PC** (optional, or hardcode):
  ```bash
  # Windows (Command Prompt)
  set VPS_BASE_URL=https://salesorder.junaidworld.com
  set VPS_API_KEY=your-secure-random-key-here

  # Windows (PowerShell)
  $env:VPS_BASE_URL="https://salesorder.junaidworld.com"
  $env:VPS_API_KEY="your-secure-random-key-here"
  ```

### 5. **Static Files & Media**

- [ ] **Collect static files on VPS**:
  ```bash
  python manage.py collectstatic --noinput
  ```

- [ ] **Verify media directory permissions**:
  ```bash
  chmod -R 755 media/
  ```

### 6. **SSL/HTTPS Configuration**

- [ ] **Ensure VPS has SSL certificate** (for HTTPS)
- [ ] **Update `VPS_BASE_URL` to use `https://`** (not `http://`)
- [ ] **Test API endpoint is accessible via HTTPS**:
  ```bash
  curl -X POST https://salesorder.junaidworld.com/sapsalesorders/sync-api-receive/ \
    -H "Content-Type: application/json" \
    -d '{"test": "connection"}'
  ```

### 7. **Testing Before Production**

#### On VPS:
- [ ] **Test API receive endpoint** (from PC or using curl):
  ```bash
  # Test endpoint is accessible
  curl -X POST https://salesorder.junaidworld.com/sapsalesorders/sync-api-receive/ \
    -H "Content-Type: application/json" \
    -d '{"orders": [], "api_so_numbers": [], "api_key": "your-api-key"}'
  ```

- [ ] **Test manual sync from web UI**:
  - Login to admin panel
  - Go to "Upload Sales Orders"
  - Click "Sync from API" button
  - Verify it works (may fail if SAP API not accessible from VPS, which is expected)

#### On PC:
- [ ] **Test sync command locally first**:
  ```bash
  python manage.py sync_salesorders_api --local-only
  ```

- [ ] **Test sync to VPS**:
  ```bash
  python manage.py sync_salesorders_api --days-back 1
  ```

- [ ] **Verify data appears on VPS**:
  - Check sales order list
  - Verify VAT numbers are populated
  - Check that orders are synced correctly

### 8. **Scheduling the Sync (PC Side)**

Set up automatic sync on your PC:

#### Windows Task Scheduler:
- [ ] **Create scheduled task** to run every 5-10 minutes:
  ```cmd
  # Command to run:
  cd D:\dataanalyst\salesorder-web\salesorder
  python manage.py sync_salesorders_api
  ```

#### Or use a Python scheduler script:
- [ ] Create `sync_scheduler.py`:
  ```python
  import schedule
  import time
  import subprocess
  
  def sync_job():
      subprocess.run(['python', 'manage.py', 'sync_salesorders_api'], 
                     cwd='D:\\dataanalyst\\salesorder-web\\salesorder')
  
  schedule.every(5).minutes.do(sync_job)
  
  while True:
      schedule.run_pending()
      time.sleep(60)
  ```

### 9. **Monitoring & Logging**

#### PC Sync Script Logging
- [ ] **Log file location**: `salesorder/logs/sync_salesorders.log`
  - Logs are automatically created in the `logs/` directory
  - Log rotation: 10 MB per file, keeps 5 backup files
  - Format: `YYYY-MM-DD HH:MM:SS | LEVEL | Message`

- [ ] **View recent sync logs**:
  ```bash
  # Windows (Command Prompt)
  type logs\sync_salesorders.log | more
  
  # Windows (PowerShell)
  Get-Content logs\sync_salesorders.log -Tail 50
  
  # View last 100 lines
  Get-Content logs\sync_salesorders.log -Tail 100
  ```

- [ ] **Check for errors in logs**:
  ```bash
  # Find errors
  findstr /i "error failed exception" logs\sync_salesorders.log
  ```

#### VPS Logging
- [ ] **Check Django logs on VPS**:
  ```bash
  # Check for errors
  tail -f /path/to/logs/django.log
  ```

- [ ] **Monitor sync success/failures**:
  - Check sync statistics in web UI
  - Monitor for API errors
  - Check database for `last_synced_at` timestamps
  - **Check PC log file**: `salesorder/logs/sync_salesorders.log`

- [ ] **Set up error alerts** (optional):
  - Email notifications on sync failures
  - Telegram/WhatsApp alerts
  - Monitor log file for ERROR entries

### 10. **Backup Strategy**

- [ ] **Database backup before deployment**:
  ```bash
  # SQLite
  cp db.sqlite3 db.sqlite3.backup-$(date +%Y%m%d)
  
  # PostgreSQL
  pg_dump salesorder_db > backup_$(date +%Y%m%d).sql
  ```

- [ ] **Set up regular backups** (daily recommended)

### 11. **Performance Considerations**

- [ ] **Database indexes** (already added):
  - `SAPSalesorder`: `so_number`, `customer_code`, `created_at`
  - `SAPSalesorderItem`: `item_no`, `pending_amount`, `row_status`

- [ ] **Cache configuration** (if using Redis/Memcached):
  - Manufacturer lookup cache (5 minutes) is already implemented

- [ ] **Monitor database size** and query performance

### 12. **Security Checklist**

- [ ] **Change default API key** (don't use the one in code)
- [ ] **Use HTTPS only** (no HTTP in production)
- [ ] **Restrict API endpoint** (consider IP whitelist if possible)
- [ ] **Review file permissions**:
  ```bash
  chmod 600 .env  # Protect .env file
  chmod 755 media/
  ```

### 13. **Post-Deployment Verification**

After deployment, verify:

- [ ] **Web UI loads correctly**
- [ ] **Sales order list displays correctly**
- [ ] **API sync endpoint responds** (test from PC)
- [ ] **VAT numbers are populated** from API
- [ ] **Calculations are correct** (totals, discounts, VAT)
- [ ] **PI creation still works**
- [ ] **PDF generation works**
- [ ] **Excel upload fallback still works**

### 14. **Rollback Plan**

If something goes wrong:

- [ ] **Keep backup of previous version**
- [ ] **Database backup before migration**
- [ ] **Document rollback steps**:
  ```bash
  # Restore database
  # Revert code changes
  # Restart services
  ```

---

## 📝 Quick Reference

### Key URLs:
- **VPS Web UI**: `https://salesorder.junaidworld.com`
- **API Sync Endpoint**: `https://salesorder.junaidworld.com/sapsalesorders/sync-api-receive/`
- **PC SAP API**: `http://192.168.1.103/IntegrationApi/api/SalesOrder` (local only)

### Key Commands:

**On VPS:**
```bash
cd salesorder
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py runserver  # Or use gunicorn/uwsgi
```

**On PC:**
```bash
cd salesorder
python manage.py sync_salesorders_api
python manage.py sync_salesorders_api --days-back 7
python manage.py sync_salesorders_api --date 2026-01-21

# Check logs
type logs\sync_salesorders.log | more
# Or in PowerShell:
Get-Content logs\sync_salesorders.log -Tail 50
```

### Important Files to Update:
1. `salesorder/settings.py` - Security settings, API URLs
2. `.env` - Environment variables (create on VPS)
3. `salesorder/so/management/commands/sync_salesorders_api.py` - PC script config

---

## ⚠️ Common Issues & Solutions

### Issue: API sync fails with "Connection refused"
- **Solution**: Check `VPS_BASE_URL` is correct and uses `https://`
- **Solution**: Verify VPS firewall allows incoming connections

### Issue: "Invalid API key" error
- **Solution**: Ensure `VPS_API_KEY` matches exactly in both PC script and VPS settings

### Issue: VAT numbers not showing
- **Solution**: Verify API response includes `BusinessPartner.FederalTaxID`
- **Solution**: Check database migration ran successfully

### Issue: Database locked (SQLite)
- **Solution**: Consider migrating to PostgreSQL for production
- **Solution**: Check for concurrent database access

---

## 📞 Support

If you encounter issues:
1. Check Django logs
2. Check sync command output
3. Verify API responses
4. Test with `--local-only` flag first
