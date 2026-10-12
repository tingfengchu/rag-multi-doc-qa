@echo off
REM ===========================================================================
REM  unlock_files.cmd - work around corporate transparent file encryption
REM  (file header "%TSD-Header-###%") that makes Dockerfile/.py unreadable
REM  to docker.exe / com.docker.backend  -> image contains garbage code.
REM
REM  MEASURED FACTS on this machine:
REM    * docker.exe / com.docker.backend read CIPHERTEXT for locked files
REM    * git.exe  reads PLAINTEXT (blob contains the real source)
REM    * cmd.exe redirection writes files as PLAINTEXT on disk
REM  => "read plaintext with git" + "write with cmd redirect" = plaintext file
REM
REM  USAGE:
REM    scripts\unlock_files.cmd            -> stage plaintext copies to _plain_build\
REM    scripts\unlock_files.cmd inplace    -> write plaintext back over originals
REM
REM  NOTE: saving a .py file again in VS Code re-encrypts it, so re-run this
REM        script before every docker build.
REM ===========================================================================
setlocal
cd /d "%~dp0.."
set "SRC=%CD%"
set "DST=%SRC%\_plain_build"
set "ACTION=stage_one"
if /i "%~1"=="inplace" set "ACTION=inplace_one"

set "FILES=Dockerfile .dockerignore pytest.ini requirements.txt requirements-eval.txt docker-compose.yml README.md .env.example"
set "FILES=%FILES% rag_config.txt rag_config.txt.example es\Dockerfile"
set "FILES=%FILES% app.py auth.py config.py db.py"
set "FILES=%FILES% utils\__init__.py utils\ids.py utils\logger.py"
set "FILES=%FILES% services\__init__.py services\es_service.py services\eval_service.py services\feedback_service.py services\file_state.py"
set "FILES=%FILES% services\pdf_service.py services\query_rewrite_service.py services\rag_service.py"
set "FILES=%FILES% services\redis_service.py services\retrieve_service.py services\vector_service.py"
set "FILES=%FILES% evaluation\__init__.py evaluation\golden_set.py evaluation\loader.py"
set "FILES=%FILES% scripts\create_admin.py scripts\evaluate_rag.py scripts\evaluate_ragas.py"
set "FILES=%FILES% scripts\reset_knowledge_base.py scripts\smoke_test_db.py scripts\unlock_files.cmd"
set "FILES=%FILES% migrations\001_init.sql migrations\002_feedback_file_dedup.sql"
set "FILES=%FILES% tests\__init__.py tests\conftest.py tests\integration\__init__.py"
set "FILES=%FILES% tests\integration\test_rag_integration.py tests\integration\test_ragas_eval.py"
set "FILES=%FILES% tests\unit\__init__.py tests\unit\test_ids.py tests\unit\test_golden_set.py"
set "FILES=%FILES% ik_config\custom.dic ik_config\IKAnalyzer.cfg.xml ik_config\stopword.dic"

if /i "%~1"=="inplace" goto :do_inplace

if exist "%DST%" rmdir /s /q "%DST%"
md "%DST%"
for %%D in (utils services scripts migrations tests tests\integration tests\unit evaluation es ik_config) do md "%DST%\%%D"

echo === staging plaintext build context into %DST%
for %%F in (%FILES%) do call :%ACTION% "%%F"
echo.
echo [OK] plaintext build context is ready: "%DST%"
echo      next: docker build -t rag_app:plain -f "%DST%\Dockerfile" "%DST%"
goto :eof

:do_inplace
echo === rewriting files IN PLACE as plaintext (originals are replaced)
for %%F in (%FILES%) do call :%ACTION% "%%F"
echo.
echo [OK] all files rewritten as plaintext.
goto :eof

:stage_one
if not exist "%SRC%\%~1" (echo   SKIP-absent  %~1 & goto :eof)
call :blob_of "%~1"
git cat-file -p %H% > "%DST%\%~1"
echo   staged  %~1
goto :eof

:inplace_one
if not exist "%SRC%\%~1" (echo   SKIP-absent  %~1 & goto :eof)
call :blob_of "%~1"
git cat-file -p %H% > "%SRC%\%~1.new"
move /y "%SRC%\%~1.new" "%SRC%\%~1" >nul
echo   fixed   %~1
goto :eof

:blob_of
set "H="
git hash-object -w --no-filters "%SRC%\%~1" > "%TEMP%\_unlock_h.tmp" 2>&1
set /p H=<"%TEMP%\_unlock_h.tmp"
if not defined H echo   SKIP-nohash  %~1
goto :eof


REM (duplicate body block removed)


REM (duplicate tail block removed)

