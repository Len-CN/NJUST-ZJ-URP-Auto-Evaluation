@echo off
chcp 65001 >nul
REM ================================================================
REM  紫金 URP 自动评估软件 - 打包脚本（生成单文件 exe）
REM  双击运行本文件即可。需先 pip 安装依赖与 pyinstaller。
REM ================================================================

echo [1/3] 检查并安装依赖...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo [2/3] 开始打包（首次较慢，请耐心等待）...
REM --collect-all 把 ddddocr 的模型文件、onnxruntime 的 DLL、opencv 一并收进 exe
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "紫金URP自动评估" ^
  --collect-all ddddocr ^
  --collect-all onnxruntime ^
  --collect-all cv2 ^
  --collect-all PIL ^
  urp_auto.py

echo.
echo [3/3] 完成！exe 在 dist\ 目录下：dist\紫金URP自动评估.exe
pause
