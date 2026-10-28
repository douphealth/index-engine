@echo off
rem gearuptofit recover-first cleanup, start to finish, in the order AGENTS.md requires: canary page, look at it, the rest of batch 1, batch 2, verify.
rem Every step stops this window on an error. Undo any batch with the --rollback command the run prints.
setlocal
set "S=D:\Claude\SEO-Setup\index-engine\fixes\gutf_recover_20261028.py"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo.
echo === Step 1 of 4: dry run (reads only). Read every "cuts:" line. ===
python "%S%" || exit /b 1
echo.
echo Press Ctrl+C to stop here, or
pause

echo.
echo === Step 2 of 4: batch 1, canary page only ===
python "%S%" --batch 1 --apply --canary-only || exit /b 1
echo.
echo Open the page it just changed in a browser (add ?nc=1 to the address) and look at it. Then
pause

echo.
echo === Step 3 of 4: the rest of batch 1 ===
python "%S%" --batch 1 --apply || exit /b 1
echo.
echo === Step 4 of 4: batch 2 ===
python "%S%" --batch 2 --apply || exit /b 1

echo.
echo === Verify (read-only) ===
python "%S%" --verify
echo.
echo Done. Purge Seraphinite then Cloudflare if you want visitors to see it sooner. Tell Claude "verify" and it will re-check the live pages.
pause
