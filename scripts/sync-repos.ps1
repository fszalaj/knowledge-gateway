<#
.SYNOPSIS
Clone or fast-forward the source repositories listed in repos.txt into repos\<name>\ (Windows PowerShell 5.1+).

.DESCRIPTION
Missing clone: git clone -c core.autocrlf=false <base><name> repos/<name>.
Existing clone: skipped with a warning when 'git status --porcelain' is not empty; otherwise
'git fetch --prune --quiet origin', and 'git merge --ff-only origin/<default>' when the clone is on its default
branch. Extra refs from repos.txt are only fetched (read them as origin/<ref>).
The script never resets, stashes, cleans, switches branches or forces anything. Clone URLs are <base><name>: the
base is the environment variable REPOS_GIT_BASE when set, else the "# git-base: <url>" line of repos.txt (for
example "# git-base: https://github.com/<owner>/").
Exit status 1 when any repository failed, 2 for usage errors.

.PARAMETER Only
Sync only the repository with this name.

.PARAMETER DryRun
Print what would be done without cloning, fetching or merging.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync-repos.ps1 -Only azure -DryRun
#>
[CmdletBinding()]
param(
    [string]$Only = '',
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$list = Join-Path $root 'repos.txt'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('sync-repos: git is not on PATH')
    exit 2
}
if (-not (Test-Path -LiteralPath $list -PathType Leaf)) {
    [Console]::Error.WriteLine("sync-repos: $list not found (copy repos.example.txt to repos.txt)")
    exit 2
}

$base = $env:REPOS_GIT_BASE
if ([string]::IsNullOrEmpty($base)) {
    foreach ($raw in [System.IO.File]::ReadAllLines($list)) {
        if ($raw -match '^#\s*git-base:\s*(\S+)') { $base = $Matches[1]; break }
    }
}
if ([string]::IsNullOrEmpty($base)) {
    [Console]::Error.WriteLine("sync-repos: no clone base; add '# git-base: https://github.com/<owner>/' to repos.txt or set REPOS_GIT_BASE")
    exit 2
}
if (-not ($base.EndsWith('/') -or $base.EndsWith('\'))) { $base = $base + '/' }

function Write-Warn([string]$Message) {
    [Console]::Error.WriteLine("sync-repos: $Message")
}

# Run git with native stderr passed straight through (no PowerShell error records); return stdout lines.
function Invoke-Git {
    param([string[]]$GitArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git @GitArgs
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return New-Object psobject -Property @{ Code = $code; Out = @($out) }
}

function Get-HeadInfo([string]$Dir) {
    $b = Invoke-Git @('-C', $Dir, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    $branch = '(detached)'
    if ($b.Code -eq 0 -and $b.Out.Count -gt 0) { $branch = [string]$b.Out[0] }
    $s = Invoke-Git @('-C', $Dir, 'rev-parse', '--short', 'HEAD')
    $sha = '-'
    if ($s.Code -eq 0 -and $s.Out.Count -gt 0) { $sha = [string]$s.Out[0] }
    return New-Object psobject -Property @{ Branch = $branch; Sha = $sha }
}

$rows = New-Object System.Collections.ArrayList
function Add-Row([string]$Name, [string]$Branch, [string]$Sha, [string]$State) {
    [void]$rows.Add((New-Object psobject -Property @{ Name = $Name; Branch = $Branch; Sha = $Sha; State = $State }))
}

function Sync-One {
    param([string]$Name, [string]$Default, [string[]]$Extra)
    $dir = Join-Path (Join-Path $root 'repos') $Name
    $url = $base + $Name

    if (-not (Test-Path -LiteralPath $dir)) {
        if ($DryRun) {
            Add-Row $Name '-' '-' "would clone $url"
            return $true
        }
        $r = Invoke-Git @('-C', $root, 'clone', '-c', 'core.autocrlf=false', $url, "repos/$Name")
        if ($r.Code -ne 0) {
            Write-Warn "${Name}: clone failed"
            Add-Row $Name '-' '-' 'FAILED (clone)'
            return $false
        }
        $state = 'cloned'
    } else {
        $inside = Invoke-Git @('-C', $dir, 'rev-parse', '--is-inside-work-tree')
        if (-not (Test-Path -LiteralPath (Join-Path $dir '.git')) -or $inside.Code -ne 0) {
            Write-Warn "${Name}: repos/$Name exists but is not a git clone; left untouched"
            Add-Row $Name '-' '-' 'FAILED (not a git clone)'
            return $false
        }
        $status = Invoke-Git @('-C', $dir, 'status', '--porcelain')
        if ($status.Code -ne 0) {
            $h = Get-HeadInfo $dir
            Add-Row $Name $h.Branch $h.Sha 'FAILED (status)'
            return $false
        }
        if (@($status.Out | Where-Object { "$_" -ne '' }).Count -gt 0) {
            Write-Warn "${Name}: working tree has local changes; skipped (nothing fetched or merged)"
            $h = Get-HeadInfo $dir
            Add-Row $Name $h.Branch $h.Sha 'skipped (local changes)'
            return $true
        }
        $h = Get-HeadInfo $dir
        $branch = $h.Branch
        if ($DryRun) {
            if ($branch -ceq $Default) { $state = "would fetch and fast-forward $Default" }
            else { $state = "would fetch only (on $branch, not $Default)" }
            Add-Row $Name $h.Branch $h.Sha $state
            return $true
        }
        $f = Invoke-Git @('-C', $dir, 'fetch', '--prune', '--quiet', 'origin')
        if ($f.Code -ne 0) {
            Write-Warn "${Name}: fetch failed"
            Add-Row $Name $h.Branch $h.Sha 'FAILED (fetch)'
            return $false
        }
        if ($branch -ceq $Default) {
            $m = Invoke-Git @('-C', $dir, 'merge', '--ff-only', '--quiet', "origin/$Default")
            if ($m.Code -ne 0) {
                Write-Warn "${Name}: $Default cannot be fast-forwarded to origin/$Default (diverged); left as is"
                $h = Get-HeadInfo $dir
                Add-Row $Name $h.Branch $h.Sha 'FAILED (not fast-forward)'
                return $false
            }
            $state = 'up to date'
        } else {
            Write-Warn "${Name}: on $branch, not $Default; fetched only"
            $state = "fetched (on $branch)"
        }
    }

    foreach ($ref in $Extra) {
        $v = Invoke-Git @('-C', $dir, 'rev-parse', '--verify', '--quiet', "refs/remotes/origin/$ref")
        if ($v.Code -ne 0) {
            $g = Invoke-Git @('-C', $dir, 'fetch', '--quiet', 'origin', "+refs/heads/${ref}:refs/remotes/origin/$ref")
            if ($g.Code -ne 0) {
                Write-Warn "${Name}: extra ref $ref could not be fetched"
                $h = Get-HeadInfo $dir
                Add-Row $Name $h.Branch $h.Sha "FAILED (extra ref $ref)"
                return $false
            }
        }
        $short = Invoke-Git @('-C', $dir, 'rev-parse', '--short', "refs/remotes/origin/$ref")
        $state = "$state; origin/$ref $($short.Out[0])"
    }

    $h = Get-HeadInfo $dir
    Add-Row $Name $h.Branch $h.Sha $state
    return $true
}

$reposDir = Join-Path $root 'repos'
if (-not (Test-Path -LiteralPath $reposDir)) { [void](New-Item -ItemType Directory -Path $reposDir) }

$failed = $false
$matched = $false
$lineNumber = 0
foreach ($raw in [System.IO.File]::ReadAllLines($list)) {
    $lineNumber++
    $line = ($raw -split '#', 2)[0].Trim()
    if ($line -eq '') { continue }
    $fields = @($line -split '\s+')
    $name = $fields[0]
    if ($fields.Count -lt 2) {
        Write-Warn "repos.txt:${lineNumber}: '$line' has no default branch"
        $failed = $true
        continue
    }
    if ($name -notmatch '^[A-Za-z0-9._-]+$' -or $name -eq '.' -or $name -eq '..') {
        Write-Warn "repos.txt:${lineNumber}: invalid repository name '$name'"
        $failed = $true
        continue
    }
    if ($Only -ne '' -and $name -cne $Only) { continue }
    $matched = $true
    $extra = @()
    if ($fields.Count -gt 2) { $extra = $fields[2..($fields.Count - 1)] }
    if (-not (Sync-One -Name $name -Default $fields[1] -Extra $extra)) { $failed = $true }
}

if ($Only -ne '' -and -not $matched) {
    Write-Warn "'$Only' is not listed in repos.txt"
    exit 2
}

$format = '{0,-24} {1,-18} {2,-10} {3}'
Write-Output ''
Write-Output ($format -f 'name', 'branch', 'sha', 'state')
Write-Output ($format -f '----', '------', '---', '-----')
foreach ($row in $rows) {
    Write-Output ($format -f $row.Name, $row.Branch, $row.Sha, $row.State)
}

if ($failed) { exit 1 }
exit 0
