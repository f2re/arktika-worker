# HTTP API 0.1

Это локальный API, а не публичный сервис. Для программного клиента сначала POST `/api/bootstrap` с `{"key":"ключ запуска"}`, сохранить cookie. Во всех POST обязательны `Content-Type: application/json`, `X-Arktika-Request: 1`, корректные Host и Origin. Лимит тела — 4 МиБ. Без сессии доступны только статический интерфейс и `/health`. OAuth callback защищён одноразовым state, а не cookie.

## Каталог, авторизация, файлы

| Метод и путь | Вход | Выход/действие |
|---|---|---|
| GET `/api/state` | — | Операция, журнал без токенов, время, очередь, срок access/STS, режим калибровки |
| POST `/api/credentials` | token, необязательный refresh_token | Сохранение только в памяти; пустой token очищает |
| POST `/api/oauth/configure` | client_id, redirect_uri, scope | Несекретная конфигурация OAuth |
| POST `/api/oauth/start` | {} | URL входа с state/PKCE |
| GET `/oauth/callback` | code, state | Обмен кода и redirect на локальную страницу |
| POST `/api/oauth/refresh` | {} | Обновление токена при наличии refresh |
| POST `/api/search` | date YYYY-MM-DD, scope day/month, platform ARCM1/ARCM2/пусто | Фоновый поиск |
| GET `/api/calendar` | month YYYY-MM, platform, category | Состояние дней и локальных сцен |
| GET `/api/catalog` | day, platform, category, channel, epsg, offset, limit | Сеансы и идентификаторы файлов |
| GET `/api/session` | platform, time | Файлы и описание сеанса |
| POST `/api/import` | path | Импорт результатов прежнего каталога |
| POST `/api/local/import` | path | Фоновая регистрация GeoTIFF |
| GET `/api/scenes` | date (день/месяц/пусто), platform | Скачанные сцены, без внутренних файловых путей |
| POST `/api/check` | id файла | Пробное чтение фрагмента |
| POST `/api/queue` | ids: массив | Постановка в сохраняемую очередь |
| GET `/api/queue` | — | Состояния, прогресс и диагностические ошибки |
| POST `/api/queue/action` | action pause/resume/restart/remove, необязательный id | Управление загрузками |
| POST `/api/settings` | download_dir / last_date / access_mode | Локальные настройки |
| POST `/api/folders` | path | Папки сервера и свободное место |
| POST `/api/folders/create` | parent, name | Создание одной папки |
| POST `/api/open-folder` | job или {} | Открыть папку средствами ОС сервера |
| GET `/api/storage/roots` | — | Опубликованные корни S3 из каталога |
| POST `/api/storage` | bucket,prefix,cursor | Чтение разрешённого списка объектов |
| POST `/api/add-uri` | uri,size | Регистрация конкретного URI без угадывания |
| POST `/api/cancel` | {} | Отмена фоновой операции каталога/обработки |

Старые `/api/token` и `/preview/{id}` сохранены для совместимости. Основной новый интерфейс использует `/api/credentials`. S3-браузер доступен через API, отдельный расширенный экран S3 в новом интерфейсе пока не реализован.

## ГИС и наука

| Метод и путь | Вход | Выход/действие |
|---|---|---|
| GET `/api/registry` | — | Каналы, продукты, статусы, области, версия рецептов |
| GET `/api/map` | preset, width | Геометрия берегов/сетки/подписей в пикселях общей сетки |
| POST `/api/calibration` | mode,reference,channels | Калибровка новых расчётов |
| POST `/api/process` | scene,product,channel,preset,width,display_min/max | Фоновый расчёт; итог через state.result |
| GET `/api/products` | — | Готовые неизменяемые продукты |
| GET `/artifact/{id}/{name}` | ID продукта, имя из files | Разрешённый файл продукта |
| GET `/artifact/{id}/export.zip` | — | Комплект с легендой и происхождением |
| POST `/api/coordinates` | x,y,preset,width | Координаты карты → lon,lat |
| POST `/api/pixel` | product,x,y | Каналы исходника, продукт, RGBA, флаги, Солнце |
| POST `/api/route` | product,points,step_km,speed_kmh,departure | Выборка вдоль маршрута и ETA |
| GET `/route-export/{id}/{csv|geojson|json}` | — | Выгрузка маршрута |
| POST `/api/motion` | first,second,preset,channel | Фоновое экспериментальное слежение |
| GET `/api/motion-result` | — | Последний результат слежения |
| POST `/api/profile` | Явный профиль; mode=cloud_top для T(z) | Интегралы либо пересечения |
| GET `/api/diagnostic` | — | Ограниченный технический отчёт |
| POST `/api/shutdown` | {} | Пауза загрузок и завершение сервера |

`POST /api/process` не возвращает готовый растр синхронно. Клиент читает `/api/state`; после завершения `result.ok=true`, `result.result` содержит метаданные продукта. При ошибке `result.ok=false`, исходники остаются нетронутыми.

Пока один общий фоновой слот каталога/импорта/обработки и независимый загрузчик. Новый тяжёлый запрос при занятом слоте отвергается, а не запускает неограниченное число GDAL-процессов. Публичных batch/job API, многопользовательского ACL и распределённой очереди нет.


## Дополнения 0.2

`GET /api/guides` — тематические справочные легенды. `GET /api/profiles` — список импортированных профилей. `POST /api/profiles/import` — профиль JSON либо `{text, metadata}` для CSV/JSON; схема в ANALYSIS.md. `POST /api/analyse` — `{product,x,y,profile_id?,altitude_m?,cloud_confirmed?,opaque_confirmed?,delta_k?}`. Координаты x/y — координаты сетки отображаемого продукта. Ответ включает исходный пиксель, метрики, спектральные гипотезы, профильные расчёты и id воспроизводимого анализа.

`GET /analysis-export/<id>` — JSON анализа с происхождением и допущениями. `POST /api/project-points` — `{product,points:[[lon,lat],...]}`, возвращает узлы и геодезическую линию в координатах карты. `POST /api/route` дополнительно принимает profile_id и altitude_m. `POST /api/settings` поддерживает view_settings с preset/product/channel.

Все перечисленные API сохраняют проверку локальной сессии; POST требует `X-Arktika-Request`. Методика кандидатов не превращается в прогноз через имя endpoint.
