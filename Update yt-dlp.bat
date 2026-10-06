@echo off
rem YouTube changes often; run this when downloads start failing.
echo Updating yt-dlp...
echo.
py -m pip install --upgrade yt-dlp || python -m pip install --upgrade yt-dlp
echo.
echo Done. Restart Media Toolbox for the update to take effect.
pause
