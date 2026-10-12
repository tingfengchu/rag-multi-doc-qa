# =============================================================================
# 带 IK 中文分词插件的 Elasticsearch 镜像
#
# 为什么必须自建镜像？
#   官网 elasticsearch:8.11.0 不带 analysis-ik，建索引时 mapping 里的
#   ik_max_word / ik_smart 会直接 400 报错（failed to find analyzer），
#   而且中文会被 standard 分词器切成单字，BM25 精度大幅下降。
#
# 构建上下文是项目根目录（见 docker-compose.yml 的 es.build.context），
# 这样才能把 ik_config/ 里的自定义词典 COPY 进来。
# =============================================================================

ARG ES_VERSION=8.11.0
FROM docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}

ARG ES_VERSION=8.11.0
# 官方发布的 IK 插件包地址（tag 里不带 v，直接跟 ES 版本号）
ARG IK_PLUGIN_URL=https://get.infini.cloud/elasticsearch/analysis-ik/${ES_VERSION}

# 安装 IK 分词插件。--batch 表示非交互，避免构建时卡住。
# 如果公司网络访问不了 get.infini.cloud，可以改成 GitHub release：
#   --build-arg IK_PLUGIN_URL=https://github.com/infinilabs/analysis-ik/releases/download/${ES_VERSION}/elasticsearch-analysis-ik-${ES_VERSION}.zip
RUN bin/elasticsearch-plugin install --batch "${IK_PLUGIN_URL}"

# 用自定义词典/停用词覆盖 IK 自带配置。
# IK 从插件目录下的 config/ 读取 IKAnalyzer.cfg.xml。
RUN mkdir -p /usr/share/elasticsearch/plugins/analysis-ik/config
COPY ik_config/IKAnalyzer.cfg.xml /usr/share/elasticsearch/plugins/analysis-ik/config/IKAnalyzer.cfg.xml
COPY ik_config/custom.dic          /usr/share/elasticsearch/plugins/analysis-ik/config/custom.dic
COPY ik_config/stopword.dic        /usr/share/elasticsearch/plugins/analysis-ik/config/stopword.dic
