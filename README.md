# 新加坡职位监控

每天自动抓取新加坡新发布的产品/项目管理类职位,结果发布到网页。
判断"新"靠自己建库做差分:职位 ID 首次出现的那天才算新,不看平台标注的日期。

**网址**: https://hasi7758.github.io/jobwatch_singapor/

## 数据来源
- **MyCareersFuture** — 新加坡政府官方求职门户,公开接口,提供真实发布日期。
  按 6 个关键词分别搜索,比拉全量再过滤命中率高。
- **公司直连** — GovTech、Grab、Ninja Van、Nium、Stripe、Thoughtworks、Xendit、ByteDance
  等 8 家的招聘系统接口。公司发到自家系统永远早于聚合平台。

## 日常使用
打开网址即可。页面显示最近 5 天内的职位,更早的折叠在下方。
定时:每天 UTC 22:40(新加坡时间次日 06:40)。

## 改关键词
在 GitHub 网页上点开 `config.yaml`,编辑 `keywords.include` / `exclude`,提交即可。
`exclude` 优先级高于 `include`。

新加坡特有的噪音已预先排除:Relationship Manager、Wealth Manager、
Property Agent、Insurance 这类销售岗在本地招聘里量极大。

## 加公司
打开公司招聘页,点一个职位看地址栏跳到哪个域名,按 `companies.yaml`
末尾的对照表填进去。

## 注意
GitHub 规定公开仓库连续 60 天无真人活动会停用定时任务,
届时会收到邮件,去 Actions 页面点一下 Enable workflow 即可。
