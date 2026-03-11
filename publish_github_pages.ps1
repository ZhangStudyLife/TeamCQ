param(
  [string]$RepoUrl = "",
  [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$root = "D:/Downloads/Documents"

python -B "$root/export_pages.py" --state-dir "$root/.schedule_state" --out-dir "$root/docs/data"

if (-not (Test-Path "$root/.git")) {
  git -C "$root" init
  git -C "$root" branch -M $Branch
}

if ($RepoUrl) {
  $hasOrigin = (git -C "$root" remote) -contains "origin"
  if ($hasOrigin) {
    git -C "$root" remote set-url origin $RepoUrl
  }
  else {
    git -C "$root" remote add origin $RepoUrl
  }
}

git -C "$root" add .

$status = git -C "$root" status --short
if ($status) {
  git -C "$root" commit -m "Prepare GitHub Pages deployment"
}

$remotes = git -C "$root" remote
if ($remotes -contains "origin") {
  git -C "$root" push -u origin $Branch
}
else {
  Write-Host "未配置 origin 远端。已完成本地导出和本地提交。"
  Write-Host "下一步请先在 GitHub 上创建仓库，然后执行："
  Write-Host "powershell -ExecutionPolicy Bypass -File `"$root/publish_github_pages.ps1`" -RepoUrl `"https://github.com/<你的用户名>/<仓库名>.git`""
}
