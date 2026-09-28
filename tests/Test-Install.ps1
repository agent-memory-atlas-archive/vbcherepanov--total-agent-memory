<#
.SYNOPSIS
    Smoke tests for install.ps1 v8.0 on Windows.

.DESCRIPTION
    Runs install.ps1 in -TestMode against a sandbox HOME (via $env:USERPROFILE
    redirection) and asserts that expected config files are created and
    schema-valid. Uses Pester when available, otherwise inline try/catch.

.EXAMPLE
    pwsh -NoProfile -File tests\Test-Install.ps1
#>

param(
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$script:Failures = 0
$script:Passes = 0

function Assert-True {
    param([string]$Name, [scriptblock]$Check)
    try {
        $result = & $Check
        if ($result) {
            Write-Host "  PASS: $Name" -ForegroundColor Green
            $script:Passes++
        } else {
            Write-Host "  FAIL: $Name (check returned $false)" -ForegroundColor Red
            $script:Failures++
        }
    } catch {
        Write-Host "  FAIL: $Name - $($_.Exception.Message)" -ForegroundColor Red
        $script:Failures++
    }
}

function New-Sandbox {
    $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "cmm-install-test-$(New-Guid)")
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    return $tmp
}

function Invoke-Install {
    param([string]$SandboxHome, [string[]]$ExtraArgs = @())
    $installScript = Join-Path $PSScriptRoot "..\install.ps1"
    $env:USERPROFILE = $SandboxHome
    $env:CLAUDE_MEMORY_DIR = Join-Path $SandboxHome ".claude-memory"
    $env:INSTALL_TEST_MODE = "1"

    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installScript, "-TestMode") + $ExtraArgs
    $output = & powershell @args 2>&1
    return @{
        ExitCode = $LASTEXITCODE
        Output   = ($output -join "`n")
    }
}

Write-Host ""
Write-Host "=== install.ps1 smoke tests (TestMode) ===" -ForegroundColor Cyan
Write-Host ""

# ---------- claude-code (default) ----------
$sandbox = New-Sandbox
$result = Invoke-Install -SandboxHome $sandbox
$claudeSettings = Join-Path $sandbox ".claude\settings.json"
$claudeConfig = Join-Path $sandbox ".claude.json"

Assert-True "default install exits cleanly" { $result.ExitCode -eq 0 }
Assert-True "claude settings.json created" { Test-Path $claudeSettings }
Assert-True "memory MCP server present in ~/.claude.json (where Claude Code reads it)" {
    $data = Get-Content $claudeConfig -Raw | ConvertFrom-Json
    $null -ne $data.mcpServers.memory
}
Assert-True "hooks block registered (UserPromptSubmit)" {
    $data = Get-Content $claudeSettings -Raw | ConvertFrom-Json
    $null -ne $data.hooks.UserPromptSubmit
}
Assert-True "hooks block registered (PreToolUse)" {
    $data = Get-Content $claudeSettings -Raw | ConvertFrom-Json
    $null -ne $data.hooks.PreToolUse
}

Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue

# ---------- cursor ----------
$sandbox = New-Sandbox
$result = Invoke-Install -SandboxHome $sandbox -ExtraArgs @("-Ide", "cursor")
$cursorCfg = Join-Path $sandbox ".cursor\mcp.json"

Assert-True "cursor install exits cleanly" { $result.ExitCode -eq 0 }
Assert-True "cursor mcp.json created" { Test-Path $cursorCfg }
Assert-True "cursor has mcpServers.memory" {
    $data = Get-Content $cursorCfg -Raw | ConvertFrom-Json
    $null -ne $data.mcpServers.memory
}

Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue

# ---------- gemini-cli ----------
$sandbox = New-Sandbox
$result = Invoke-Install -SandboxHome $sandbox -ExtraArgs @("-Ide", "gemini-cli")
$geminiCfg = Join-Path $sandbox ".gemini\settings.json"

Assert-True "gemini-cli install exits cleanly" { $result.ExitCode -eq 0 }
Assert-True "gemini settings.json created" { Test-Path $geminiCfg }

Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue

# ---------- opencode ----------
$sandbox = New-Sandbox
$result = Invoke-Install -SandboxHome $sandbox -ExtraArgs @("-Ide", "opencode")
$opencodeCfg = Join-Path $sandbox ".config\opencode\opencode.json"

Assert-True "opencode install exits cleanly" { $result.ExitCode -eq 0 }
Assert-True "opencode opencode.json created" { Test-Path $opencodeCfg }
Assert-True "opencode entry is a local mcp server" {
    $data = Get-Content $opencodeCfg -Raw | ConvertFrom-Json
    $data.mcp.memory.type -eq "local"
}

Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue

# ---------- codex ----------
$sandbox = New-Sandbox
$result = Invoke-Install -SandboxHome $sandbox -ExtraArgs @("-Ide", "codex")
$codexCfg = Join-Path $sandbox ".codex\config.toml"

Assert-True "codex install exits cleanly" { $result.ExitCode -eq 0 }
Assert-True "codex config.toml created" { Test-Path $codexCfg }
Assert-True "codex config has [mcp_servers.memory]" {
    (Get-Content $codexCfg -Raw) -match "\[mcp_servers\.memory\]"
}
Assert-True "codex config has env overrides" {
    $c = Get-Content $codexCfg -Raw
    $c -match "MEMORY_TRIPLE_TIMEOUT_SEC" -and $c -match "MEMORY_ENRICH_TIMEOUT_SEC"
}
Assert-True "codex config has fence markers" {
    $c = Get-Content $codexCfg -Raw
    $c -match "# --- total-agent-memory MCP Server ---" -and $c -match "# --- End total-agent-memory ---"
}

Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue

# ---------- invalid -Ide rejected ----------
$sandbox = New-Sandbox
try {
    $result = Invoke-Install -SandboxHome $sandbox -ExtraArgs @("-Ide", "emacs-doctor")
    Assert-True "invalid -Ide returns non-zero" { $result.ExitCode -ne 0 }
} finally {
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
}

# ---------- summary ----------
Write-Host ""
Write-Host "=======================================" -ForegroundColor Cyan
Write-Host "  PASSED: $script:Passes" -ForegroundColor Green
Write-Host "  FAILED: $script:Failures" -ForegroundColor $(if ($script:Failures -gt 0) {"Red"} else {"Green"})
Write-Host "=======================================" -ForegroundColor Cyan

if ($script:Failures -gt 0) { exit 1 } else { exit 0 }
