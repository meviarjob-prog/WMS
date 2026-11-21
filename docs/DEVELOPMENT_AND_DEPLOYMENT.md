# WMS: внесение изменений и выкладка на сервер

Эта инструкция предназначена для передачи проекта другому сотруднику. Она
описывает полный рабочий цикл: подготовку компьютера, внесение и проверку
изменений, отправку в GitHub, обновление боевого сервера, проверку результата и
откат.

## 1. Важные данные проекта

- Репозиторий: `https://github.com/meviarjob-prog/WMS.git`
- Рабочая ветка: `claude/project-thread-19dtys`
- Каталог приложения на сервере: `/opt/wms`
- Сервис приложения: `wms`
- Конфигурация окружения сервера: `/etc/wms.env`
- Боевая база SQLite: `/opt/wms/instance/wms.db`
- Резервные копии: `/opt/wms/backups/`
- Приложение на сервере: Gunicorn на `127.0.0.1:8000`, перед ним nginx и HTTPS

Все команды ниже предполагают, что изменения отправляются именно в ветку
`claude/project-thread-19dtys`. Не выкладывайте другую ветку без отдельного
согласования.

## 2. Однократная подготовка компьютера разработчика

### 2.1. Необходимые программы

Установите:

1. Git.
2. Python 3.11 или новее.
3. Редактор кода, например VS Code.
4. SSH-клиент. В Windows 10/11 он уже встроен в PowerShell.

Проверьте установку в PowerShell:

```powershell
git --version
py --version
ssh -V
```

### 2.2. Клонирование репозитория

```powershell
cd "$HOME\Documents"
git clone https://github.com/meviarjob-prog/WMS.git
cd WMS
git fetch origin
git switch claude/project-thread-19dtys
```

Если репозиторий закрытый, GitHub попросит авторизацию. Обычный пароль аккаунта
для Git по HTTPS не используется: войдите через Git Credential Manager либо
используйте персональный токен/SSH-ключ, выданный ответственным за репозиторий.

Проверьте ветку:

```powershell
git branch --show-current
```

Команда должна вывести `claude/project-thread-19dtys`.

### 2.3. Виртуальное окружение и зависимости

В корне проекта:

```powershell
py -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
```

`Set-ExecutionPolicy` действует только в текущем окне PowerShell и нужен, если
Windows запрещает запуск `Activate.ps1`.

### 2.4. Удобное подключение к серверу без ввода полной команды

Создайте SSH-ключ на компьютере сотрудника, если его еще нет:

```powershell
ssh-keygen -t ed25519 -C "wms-developer"
```

Передайте содержимое файла `%USERPROFILE%\.ssh\id_ed25519.pub` администратору
сервера. Администратор должен добавить этот открытый ключ в
`/root/.ssh/authorized_keys` либо в профиль другого пользователя с правом
`sudo`. Закрытый файл `id_ed25519` никому не передавать.

Создайте или отредактируйте файл `%USERPROFILE%\.ssh\config`:

```text
Host wms-prod
    HostName ВАШ_IP_ИЛИ_ДОМЕН
    User root
    IdentityFile C:\Users\ИМЯ_ПОЛЬЗОВАТЕЛЯ\.ssh\id_ed25519
```

После этого подключение выполняется короткой командой:

```powershell
ssh wms-prod
```

Если сервер использует не `root`, в `User` укажите выданного пользователя. У
него должно быть право выполнять приведенные ниже команды через `sudo`.

## 3. Порядок внесения изменений

### 3.1. Перед началом работы

Откройте PowerShell в каталоге проекта и активируйте окружение:

```powershell
.\.venv\Scripts\Activate.ps1
git status
git branch --show-current
git fetch origin
git pull --ff-only origin claude/project-thread-19dtys
```

Перед `pull` рабочее дерево должно быть чистым. Если `git status` показывает
чужие или непонятные изменения, не удаляйте их и не выполняйте `reset --hard`.
Сначала выясните у автора, нужны ли они, либо сохраните их отдельным коммитом.

### 3.2. Внесение изменений

1. Изучите существующий маршрут, шаблон, модель и тесты, относящиеся к задаче.
2. Изменяйте только относящиеся к задаче файлы.
3. Не добавляйте в Git пароли, SSH-ключи, токены, `/etc/wms.env`, файлы базы
   `instance/*.db` и `instance/*.json`.
4. Если меняется поведение, добавьте или обновите автотест.
5. Если добавляется поле в существующую таблицу, добавьте его в модель и в
   штатную функцию обновления схемы приложения. При старте WMS самостоятельно
   создает отсутствующие таблицы, колонки и индексы.
6. Удаление/переименование колонок и преобразование данных нельзя делать
   простым `ALTER TABLE` без отдельного плана миграции и резервной копии.

Для локального запуска:

```powershell
python run.py
```

Откройте `http://localhost:5000`. Локальная база создается в
`instance/wms.db`; она не попадает в Git.

### 3.3. Проверка перед коммитом

Остановите локальный сервер клавишами `Ctrl+C`, затем выполните:

```powershell
python -m compileall -q wms
python -m pytest -q
git diff --check
git status --short
git diff
```

Дополнительно вручную проверьте измененный сценарий в браузере под нужными
ролями. Для мобильных функций проверьте узкий экран и сканирование по HTTPS,
если задача его затрагивает.

Если полный набор тестов временно невозможно выполнить, запустите хотя бы
тесты измененного раздела, например:

```powershell
python -m pytest -q tests\test_mvb_logistics.py
```

В описании коммита обязательно укажите, какие проверки действительно были
выполнены.

## 4. Коммит и отправка в GitHub

Сначала еще раз проверьте список файлов:

```powershell
git status --short
```

Добавьте только нужные файлы, а не весь каталог вслепую:

```powershell
git add путь\к\измененному_файлу.py путь\к\тесту.py
git diff --cached
git commit -m "Краткое описание изменения"
git push origin claude/project-thread-19dtys
```

Убедитесь, что отправлен правильный коммит:

```powershell
git log -1 --oneline
git status
```

`git status` должен сообщить, что рабочее дерево чистое и ветка синхронизирована
с `origin/claude/project-thread-19dtys`.

## 5. Выкладка на действующий сервер

### 5.1. Правило выкладки

Выкладывайте только уже отправленный в GitHub и проверенный коммит. Перед каждой
выкладкой обязательно создавайте резервную копию базы. Желательно выполнять
обновление в период, когда сотрудники не проводят документы и не сканируют
короба: перезапуск сервиса обычно короткий, но активный запрос будет прерван.

### 5.2. Подключение и предварительная проверка

```powershell
ssh wms-prod
```

На сервере:

```bash
sudo systemctl status wms --no-pager
sudo git -C /opt/wms status --short --branch
sudo git -C /opt/wms branch --show-current
sudo git -C /opt/wms log -1 --oneline
```

Ожидаемая ветка — `claude/project-thread-19dtys`. Если `status` показывает
локальные изменения на сервере, остановитесь: их нельзя затирать. Скопируйте
вывод, выясните происхождение изменений и только после этого продолжайте.

### 5.3. Резервная копия

```bash
sudo bash /opt/wms/deploy/backup.sh
sudo ls -lh /opt/wms/backups/ | tail
```

Сохраните в журнале работ имя созданного файла. Скрипт использует безопасный
механизм SQLite backup и может работать при запущенном приложении.

### 5.4. Получение кода и перезапуск

```bash
sudo git -C /opt/wms fetch origin claude/project-thread-19dtys
sudo git -C /opt/wms checkout claude/project-thread-19dtys
sudo git -C /opt/wms merge --ff-only origin/claude/project-thread-19dtys
sudo /opt/wms/.venv/bin/pip install -r /opt/wms/requirements.txt -r /opt/wms/deploy/requirements-server.txt
sudo systemctl restart wms
```

При старте приложение автоматически добавляет отсутствующие таблицы, колонки и
индексы. Отдельная команда миграции в текущем проекте не используется.

### 5.5. Обязательная проверка после перезапуска

```bash
sudo systemctl is-active wms
sudo systemctl status wms --no-pager
sudo journalctl -u wms -n 100 --no-pager
sudo git -C /opt/wms log -1 --oneline
```

`systemctl is-active wms` должен вывести `active`. В журнале не должно быть
`Traceback`, циклических перезапусков или ошибок схемы базы.

Проверьте nginx:

```bash
sudo nginx -t
sudo systemctl is-active nginx
```

Проверьте сайт с сервера, заменив домен на настоящий:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://ВАШ_ДОМЕН/mvb/login
```

Код `200` или редирект `301/302` означает, что веб-сервер отвечает. Затем
обязательно откройте сайт с обычного компьютера/телефона и вручную проверьте:

1. Вход в систему.
2. Измененный раздел и основной рабочий сценарий.
3. Права администратора, оператора и клиента, если они затронуты.
4. Создание и чтение данных.
5. Отсутствие ошибок в консоли браузера и в `journalctl`.

После проверки запишите дату, коммит из `git log -1`, имя резервной копии и
результат проверки.

## 6. Первичная установка на новый Ubuntu-сервер

Этот раздел нужен только для нового сервера. Для обычных обновлений используйте
раздел 5: он не требует повторно передавать токены Cloudflare.

Для Ubuntu 22.04/24.04, адреса `sslip.io` и текущей рабочей ветки:

```bash
curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/WMS/claude/project-thread-19dtys/deploy/setup.sh | \
  sudo env WMS_BRANCH=claude/project-thread-19dtys bash -s -- you@example.com
```

Если используется собственный домен Cloudflare:

```bash
curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/WMS/claude/project-thread-19dtys/deploy/setup.sh | \
  sudo env WMS_BRANCH=claude/project-thread-19dtys \
  WMS_DOMAIN=wms.example.com WMS_CF_API_TOKEN=СЕКРЕТНЫЙ_ТОКЕН \
  bash -s -- you@example.com
```

После установки переключите Cloudflare SSL/TLS в режим `Full (strict)`.
Токен Cloudflare является секретом: не сохраняйте его в Git, документах,
скриншотах или общем чате. По возможности удалите команду с токеном из истории
shell после установки.

Скрипт `deploy/setup.sh` можно запускать повторно, но при обновлении он выполняет
`git reset --hard origin/<ветка>`. Это уничтожит любые незакоммиченные изменения
в `/opt/wms`. Поэтому повторный запуск допустим только после проверки, что
`sudo git -C /opt/wms status --short` ничего не выводит. Для штатного обновления
безопаснее использовать раздел 5.

## 7. Откат неудачного обновления

### 7.1. Предпочтительный откат кода через Git

Не переписывайте историю рабочей ветки. На компьютере разработчика создайте
обратный коммит:

```powershell
git switch claude/project-thread-19dtys
git pull --ff-only origin claude/project-thread-19dtys
git log --oneline -10
git revert ХЕШ_ПЛОХОГО_КОММИТА
python -m pytest -q
git push origin claude/project-thread-19dtys
```

После этого снова выполните обычную выкладку из раздела 5. Такой способ сохраняет
историю и понятен следующему сотруднику.

Если сайт полностью недоступен и ждать исправления нельзя, временно остановите
сервис командой `sudo systemctl stop wms`, сообщите пользователям о работах и
привлеките ответственного за репозиторий. Не выполняйте наугад `reset --hard`.

### 7.2. Восстановление базы

Базу восстанавливают только если обновление повредило данные, а не при обычной
ошибке интерфейса. Все операции выполняются на остановленном сервисе. Сначала
зафиксируйте точный путь к резервной копии:

```bash
sudo ls -lht /opt/wms/backups/wms_*.db | head
sudo systemctl stop wms
```

Не удаляйте текущую базу. Переместите ее в отдельный файл с конкретной датой,
затем скопируйте выбранную резервную копию:

```bash
sudo mv /opt/wms/instance/wms.db /opt/wms/instance/wms.db.failed-ГГГГММДД-ЧЧММ
sudo cp /opt/wms/backups/wms_ГГГГ-ММ-ДД_ЧЧ-ММ-СС.db /opt/wms/instance/wms.db
sudo chown wms:wms /opt/wms/instance/wms.db
sudo systemctl start wms
sudo systemctl status wms --no-pager
sudo journalctl -u wms -n 100 --no-pager
```

Перед выполнением замените все шаблонные имена на реально существующие пути и
еще раз проверьте их командой `ls`. Файлы `wms.db-wal` и `wms.db-shm`, если они
остались после остановки, не подключайте к восстановленной базе: их должен
проверить и перенести в архив администратор.

## 8. Быстрая диагностика проблем

### Сервис не запускается

```bash
sudo systemctl status wms --no-pager
sudo journalctl -u wms -n 200 --no-pager
```

Ищите первый `Traceback`, а не последующие сообщения о перезапуске.

### Ошибка 502 Bad Gateway

```bash
sudo systemctl is-active wms
sudo ss -ltnp | grep 8000
sudo tail -n 100 /var/log/nginx/error.log
```

Обычно это означает, что Gunicorn не запустился или не слушает порт 8000.

### `ModuleNotFoundError`

```bash
sudo /opt/wms/.venv/bin/pip install -r /opt/wms/requirements.txt -r /opt/wms/deploy/requirements-server.txt
sudo systemctl restart wms
```

### Ошибка доступа к базе

```bash
sudo ls -la /opt/wms/instance
sudo chown wms:wms /opt/wms/instance/wms.db
sudo systemctl restart wms
```

Не меняйте права рекурсивно на весь сервер и не выдавайте базе режим `777`.

### На экране осталась старая версия

Сначала убедитесь, что на сервере нужный коммит:

```bash
sudo git -C /opt/wms log -1 --oneline
```

Затем обновите страницу без кеша (`Ctrl+F5`). На телефоне полностью закройте
вкладку/PWA и откройте снова. Если проблема остается, проверьте сервис-воркер и
логи браузера, а не выполняйте повторную выкладку вслепую.

### Git сообщает о локальных изменениях или конфликте

Не применяйте `reset --hard` и не удаляйте файлы. Выполните:

```bash
sudo git -C /opt/wms status
sudo git -C /opt/wms diff
```

Сохраните вывод и передайте ответственному. На боевом сервере не должно быть
ручной разработки; изменения должны приходить через GitHub.

## 9. Чек-лист ответственного за выпуск

Перед выкладкой:

- [ ] Выбрана ветка `claude/project-thread-19dtys`.
- [ ] Изменения проверены через `git diff`.
- [ ] Автотесты прошли.
- [ ] Коммит отправлен в GitHub.
- [ ] На сервере нет локальных изменений.
- [ ] Создана и записана свежая резервная копия базы.

После выкладки:

- [ ] `wms` и `nginx` имеют статус `active`.
- [ ] В журнале нет новых ошибок.
- [ ] На сервере установлен ожидаемый хеш коммита.
- [ ] Сайт открывается извне.
- [ ] Измененный сценарий проверен под нужными ролями.
- [ ] В журнале выпуска записаны дата, коммит, резервная копия и результат.

## 10. Что передать следующему сотруднику отдельно

Не храните секреты в этом документе. Через защищенный канал передайте:

1. IP/домен боевого сервера.
2. Имя SSH-пользователя и добавьте открытый SSH-ключ сотрудника на сервер.
3. Доступ к репозиторию GitHub.
4. Адрес сайта и отдельную учетную запись администратора WMS.
5. Контакт ответственного за домен/Cloudflare и серверный аккаунт.
6. Место ведения журнала выпусков и порядок согласования времени выкладки.

Пароли, приватные SSH-ключи, содержимое `/etc/wms.env`, Cloudflare API token и
Google service-account JSON нельзя пересылать открытым сообщением или добавлять
в репозиторий.
