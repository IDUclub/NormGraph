## v0.11.0 (2026-10-05)

[feat/explanation-context](https://github.com/IDUclub/NormGraph/pull/52) (#52)

- feat: show explanations as context of the clauses they explain
- fix: calibrate the explanation similarity threshold on IDU_DVD scores
- style: format the explanation live test
- ci: let auto-merge bump the version after an auto-format commit

## v0.10.0 (2026-10-05)

[feat/amendment-carry](https://github.com/IDUclub/NormGraph/pull/51) (#51)

- feat: re-extract only the changed clauses of a new edition

## v0.9.0 (2026-10-03)

[feat/red-line-not-in-data](https://github.com/IDUclub/NormGraph/pull/50) (#50)

- feat: явная причина red_line_not_in_data для норм о красных линиях

## v0.8.0 (2026-10-03)

[feat/norm-dedup-kinds](https://github.com/IDUclub/NormGraph/pull/49) (#49)

- feat: закрытый список видов норм и группировка дублей

## v0.7.0 (2026-10-03)

[feat/norm-reference-context](https://github.com/IDUclub/NormGraph/pull/48) (#48)

- feat: учитывать связанные пункты и ссылки при извлечении норм и построении планов

## v0.6.0 (2026-10-02)

[feat/admin-restrictions-view](https://github.com/IDUclub/NormGraph/pull/47) (#47)

- feat: просмотр извлечённых ограничений с фильтрами в панели администратора

## v0.5.2 (2026-10-02)

[fix/version-status-token](https://github.com/IDUclub/NormGraph/pull/46) (#46)

- ci: ставить итоговый статус version токеном VERSION_STATUS_TOKEN, чтобы мердж запускал выкатку на dev

## v0.5.1 (2026-10-02)

[chore/versioning-policy](https://github.com/IDUclub/NormGraph/pull/45) (#45)

- ci: поднимать версию при каждом мердже в dev, релиз в main только ставит тег
- ci: поднимать версию в ветке PR по включению auto-merge

## v0.5.0 (2026-10-01)

### Feat

- тематический фильтр норм и список документов для проверки соответствия (#32)
- массовый перепарсинг норм и планов из админ-панели (#28)
- add NormGraph administration panel (#26)
- add extraction backfill for documents without restrictions (#19)
- **auth**: require service tokens (#8)
- - updated gitignore

### Fix

- предлагать названия слоёв планов как кандидатов темы проверки соответствия (#33)
- подхватывать новую метку версии документа при сверке с IDU_DVD (#31)
- постраничный листинг ограничений и точный поиск для вопросов о размещении (#30)
- use direct distance for education checks without explicit routes (#27)
- ground spatial norm extraction and check plans (#24)
- allow unauthenticated log and settings reads (#23)
- enforce Giga 2048 indexes and parallelize extraction (#7)

## v0.4.0 (2026-09-25)

### Feat

- тематический фильтр норм и список документов для проверки соответствия (#32)

### Fix

- предлагать названия слоёв планов как кандидатов темы проверки соответствия (#33)
- подхватывать новую метку версии документа при сверке с IDU_DVD (#31)
- постраничный листинг ограничений и точный поиск для вопросов о размещении (#30)

## v0.3.0 (2026-09-22)

### Feat

- массовый перепарсинг норм и планов из админ-панели (#28)
- add NormGraph administration panel (#26)
- add extraction backfill for documents without restrictions (#19)

### Fix

- use direct distance for education checks without explicit routes (#27)
- ground spatial norm extraction and check plans (#24)
- allow unauthenticated log and settings reads (#23)

## v0.2.0 (2026-08-18)

### Feat

- **auth**: require service tokens (#8)

### Fix

- enforce Giga 2048 indexes and parallelize extraction (#7)

## v0.1.0 (2026-08-18)

### Feat

- - updated gitignore
