try {
    $ErrorActionPreference = 'Stop'
    $distro = $env:HARNESS_TEAM_DISTRO
    if ($distro -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9 ._-]*\z' -or $distro.Trim() -ne $distro) { exit 1 }
    $bin = Join-Path $env:LOCALAPPDATA 'HarnessMailbox\bin'
    $shim = Join-Path $bin 'team.cmd'
    $prefix = '"%SystemRoot%\System32\wsl.exe" --distribution "{0}" --cd ~ --exec ' -f $distro
    $launch = $prefix + '/usr/bin/env python3 .local/bin/team --from-windows'
    $content = "@echo off`r`n$launch`r`nexit /b %errorlevel%`r`n"
    $previousLaunch = $prefix + '/bin/sh -lc "exec ~/.local/bin/team"'
    $previousContent = "@echo off`r`n$previousLaunch`r`nexit /b %errorlevel%`r`n"
    # WSL interop may start this process with an older snapshot of Windows PATH.
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($env:Path, $machinePath, $userPath, $bin) -join [IO.Path]::PathSeparator
    foreach ($existing in @(Get-Command team -All -ErrorAction SilentlyContinue)) {
        if ($existing.CommandType -ne 'Application' -or $existing.Path -ne $shim) { exit 1 }
    }
    $installed = $null
    if (Test-Path -LiteralPath $shim) {
        $installed = [IO.File]::ReadAllText($shim)
        if ($installed -cne $content -and $installed -cne $previousContent) { exit 1 }
    }
    if ($env:HARNESS_TEAM_CHECK -eq '1') {
        $registered = @(([string]$userPath -split ';') | ForEach-Object {
            [Environment]::ExpandEnvironmentVariables($_.Trim().Trim('"')).TrimEnd('\')
        })
        if ($installed -cne $content -or $bin -notin $registered) { exit 1 }
        exit 0
    }
    if ($installed -cne $content) {
        [void][IO.Directory]::CreateDirectory($bin)
        $temp = Join-Path $bin ([Guid]::NewGuid().ToString() + '.tmp')
        try {
            [IO.File]::WriteAllText($temp, $content, [Text.Encoding]::ASCII)
            if ($null -eq $installed) {
                [IO.File]::Move($temp, $shim)
            } else {
                [IO.File]::Replace($temp, $shim, [NullString]::Value)
            }
        } finally {
            [IO.File]::Delete($temp)
        }
    }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $raw = $key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($null -ne $raw) {
            $kind = $key.GetValueKind('Path')
            if ($kind -notin 'String', 'ExpandString') { exit 1 }
        }
        $entries = @(([string]$raw -split ';') | ForEach-Object {
            [Environment]::ExpandEnvironmentVariables($_.Trim().Trim('"')).TrimEnd('\')
        })
        if ($bin -notin $entries) {
            $newPath = $bin
            if ($raw) { $newPath = $raw + ';' + $bin }
            $key.SetValue('Path', $newPath, $kind)
            # Ask Explorer to refresh the environment for newly opened terminals.
            $signature = '[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);'
            try {
                Add-Type -Namespace HarnessMailbox -Name Shell -MemberDefinition $signature
                $result = [UIntPtr]::Zero
                [void][HarnessMailbox.Shell]::SendMessageTimeout([IntPtr]65535, 26, [UIntPtr]::Zero, 'Environment', 2, 1000, [ref]$result)
            } catch {
                # The persisted PATH also takes effect at the next Windows sign-in.
            }
        }
    } finally {
        $key.Dispose()
    }
    exit 0
} catch {
    exit 1
}

# The empty line above submits the block to PowerShell when it reads commands from stdin.
