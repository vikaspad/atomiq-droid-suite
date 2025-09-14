@echo off
setlocal
cd /d %~dp0

if not exist node_modules (
  call npm install
)

echo Starting Angular dev server: http://localhost:4200
call npx ng serve --proxy-config proxy.conf.json
endlocal