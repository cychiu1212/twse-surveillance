# TWSE Radar one-shot deploy script (ASCII only to avoid encoding issues)
# Usage: powershell -ExecutionPolicy Bypass -File C:\Users\Charlene\twse-surveillance\deploy.ps1

$gh = "C:\Program Files\GitHub CLI\gh.exe"
$repo = "twse-surveillance"
Set-Location "C:\Users\Charlene\twse-surveillance"

Write-Host "=== 1/5 check login ===" -ForegroundColor Cyan
$user = & $gh api user --jq .login
if (-not $user) { Write-Host "ERROR: gh not logged in. Run: gh auth login" -ForegroundColor Red; exit 1 }
Write-Host "logged in as: $user"

Write-Host "=== 2/5 create repo + push ===" -ForegroundColor Cyan
$exists = & $gh repo view "$user/$repo" --json name 2>$null
if ($exists) {
    Write-Host "repo exists, pushing..."
    git remote get-url origin 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { git remote add origin "https://github.com/$user/$repo.git" }
    git push -u origin main
} else {
    & $gh repo create $repo --public --source . --push --description "TWSE disposal radar"
}
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: push failed" -ForegroundColor Red; exit 1 }

Write-Host "=== 3/5 set SITE_PASSWORD secret ===" -ForegroundColor Cyan
$pw = (Get-Content "site_password.txt" -Raw).Trim()
$pw | & $gh secret set SITE_PASSWORD --repo "$user/$repo"

Write-Host "=== 4/5 enable GitHub Pages (main /docs) ===" -ForegroundColor Cyan
& $gh api -X POST "repos/$user/$repo/pages" -f "source[branch]=main" -f "source[path]=/docs" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $gh api -X PUT "repos/$user/$repo/pages" -f "source[branch]=main" -f "source[path]=/docs" 2>$null
}

Write-Host "=== 5/5 trigger first data update ===" -ForegroundColor Cyan
& $gh workflow run "update.yml" --repo "$user/$repo" 2>$null

Write-Host ""
Write-Host "DONE! Site URL (first build takes 1-3 min):" -ForegroundColor Green
Write-Host "  https://$user.github.io/$repo/" -ForegroundColor Yellow
Write-Host "  password: $pw"
