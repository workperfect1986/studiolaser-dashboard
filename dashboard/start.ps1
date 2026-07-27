# Inicia o dashboard com captação em tempo real do Nucleus.
# Ajuste a URL base se o domínio mudar.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $env:NUCLEUS_BASE_URL) {
  $env:NUCLEUS_BASE_URL = "https://studiolaser.nucleusapp.com.br"
}
if (-not $env:NUCLEUS_EMAIL -or -not $env:NUCLEUS_PASSWORD) {
  Write-Host "Defina NUCLEUS_EMAIL e NUCLEUS_PASSWORD antes de iniciar."
  Write-Host "Exemplo:"
  Write-Host '  $env:NUCLEUS_EMAIL = "seu@email.com"'
  Write-Host '  $env:NUCLEUS_PASSWORD = "sua-senha"'
  exit 1
}
if (-not $env:NUCLEUS_OPERADOR_ID) {
  $env:NUCLEUS_OPERADOR_ID = "7012"
}
if (-not $env:NUCLEUS_USER_ID) {
  $env:NUCLEUS_USER_ID = "7012"
}
if (-not $env:NUCLEUS_DATE_DE) {
  $env:NUCLEUS_DATE_DE = "01/01/2026"
}
if (-not $env:NUCLEUS_DATE_ATE) {
  $env:NUCLEUS_DATE_ATE = "31/12/2026"
}
if (-not $env:NUCLEUS_CACHE_TTL) {
  $env:NUCLEUS_CACHE_TTL = "60"
}
if (-not $env:PORT) {
  $env:PORT = "8765"
}

Write-Host "Base URL: $($env:NUCLEUS_BASE_URL)"
Write-Host "Abrindo http://127.0.0.1:$($env:PORT)"
python server.py
