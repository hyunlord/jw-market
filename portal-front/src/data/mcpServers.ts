// MCP 서버 정보 — 퍼블 MCP_server.html의 하드코딩 DATA를 그대로 옮긴 정적 데이터.
// 24개 서버, 각 서버별 tool 목록(name/desc). 문서 정보는 추후 API 연동(대기).

export interface McpTool {
  name: string
  desc: string
}

export interface McpServer {
  /** 시스템 식별자 (예: "nci-gdc") — React key */
  sys: string
  /** 표시명 (예: "NCI GDC") */
  nameKo: string
  /** 서버 설명 (사이드바 카드 본문) */
  desc: string
  /** 제공 tool 목록 */
  tools: McpTool[]
}

export const MCP_SERVERS: McpServer[] = [
  {
    "sys": "nci-gdc",
    "nameKo": "NCI GDC",
    "desc": "NCI Genomic Data Commons(GDC)의 암 유전체 데이터를 확인할 수 있습니다. TCGA/TARGET 코호트 전반의 체세포 변이(somatic mutation)·CNV(복제수 변이)·유전자 발현·임상·인구통계 데이터를 조회할 때 활용합니다.",
    "tools": [
      {
        "name": "gdc_field_info",
        "desc": "GDC Data Dictionary 스키마에서 특정 필드의 정의를 확인할 수 있습니다. 필드명(예: \"age_at_diagnosis\")을 입력하면 해당 필드의 엔티티 타입·설명·데이터 타입·접근 경로·허용 값·예시 값을 살펴볼 수 있어, family_histories·diagnoses·demographic 등 연관 엔티티에 존재하는 필드까지 파악할 때 활용합니다."
      },
      {
        "name": "gdc_quick_count",
        "desc": "필터 조건에 일치하는 레코드의 개수를 빠르게 확인할 수 있습니다. files·cases·projects 등 엔티티별로 전체 건수를 파악할 수 있고, disease_type·primary_site 등으로 그룹별 집계도 함께 살펴볼 수 있습니다."
      },
      {
        "name": "gdc_graphql_query",
        "desc": "GDC GraphQL API로 엔티티 간 관계와 특정 필드를 직접 조회할 수 있습니다. 케이스·파일·변이 등 서로 연결된 데이터를 함께 살펴볼 때 활용합니다."
      },
      {
        "name": "gdc_rest_query",
        "desc": "GDC의 모든 주요 엔티티(files·cases·projects·genes·ssms·ssm_occurrences·cnv_occurrences·annotations)에 걸쳐 데이터를 검색할 수 있는 핵심 도구입니다. 원하는 필터 조건으로 케이스·파일·변이 정보를 폭넓게 조회하고 집계까지 확인할 때 활용합니다."
      },
      {
        "name": "gdc_ssms",
        "desc": "단순 체세포 변이(Simple Somatic Mutation, SSM) 레코드를 확인할 수 있습니다. 유전자 심볼(예: \"TP53\", \"KRAS\")로 변이를 찾으면 변이별 유전체 좌표·대립유전자 변화·기능적 영향(consequence)을 살펴볼 수 있습니다."
      },
      {
        "name": "gdc_ssm_occurrences",
        "desc": "특정 체세포 변이가 어떤 케이스에서 관찰되는지 확인할 수 있습니다. 유전자 심볼이나 프로젝트(예: \"TCGA-BRCA\")로 케이스-변이 연계 정보를 조회하여 변이가 나타난 환자 케이스를 파악할 때 활용합니다."
      },
      {
        "name": "gdc_cnv_occurrences",
        "desc": "케이스 수준의 복제수 변이(CNV) 이벤트를 확인할 수 있습니다. 유전자 심볼(예: \"ERBB2\")이나 프로젝트로 케이스-유전자별 증폭(gain)·손실(loss) 이벤트를 조회할 때 활용합니다."
      },
      {
        "name": "gdc_genes",
        "desc": "유전자 메타데이터를 확인할 수 있습니다. 유전자 심볼(예: \"TP53\", \"BRCA1\")로 조회하면 Ensembl ID·biotype·유전체 좌표·기능적 주석을 살펴볼 수 있습니다."
      },
      {
        "name": "gdc_top_mutated_genes_by_project",
        "desc": "특정 GDC 프로젝트(예: \"TCGA-BRCA\")에서 가장 빈번하게 변이된 유전자 목록을 확인할 수 있습니다. 유전자 심볼·Ensembl ID와 함께 변이를 가진 프로젝트별 케이스 수를 살펴볼 때 활용합니다."
      },
      {
        "name": "gdc_top_cases_counts_by_genes",
        "desc": "지정한 유전자(Ensembl ID, 예: \"ENSG00000141510\")에 대한 프로젝트별 케이스 수를 확인할 수 있습니다. 해당 유전자 변이가 어떤 프로젝트에 얼마나 분포하는지 파악할 때 활용합니다."
      },
      {
        "name": "gdc_annotations",
        "desc": "케이스·샘플·파일에 연결된 데이터 품질 주석을 확인할 수 있습니다. 엔티티 ID나 타입으로 조회하면 품질 플래그·임상 고지·처리 경고 등 category·classification·notes를 살펴볼 수 있습니다."
      },
      {
        "name": "gdc_history",
        "desc": "특정 파일의 버전 이력을 확인할 수 있습니다. 파일 UUID를 입력하면 버전 번호·릴리스 날짜·변경 메타데이터 등 출처(provenance) 정보와 변경 타임라인을 살펴볼 수 있습니다."
      },
      {
        "name": "gdc_survival_analysis",
        "desc": "Kaplan-Meier 생존 데이터를 확인할 수 있습니다. 코호트 필터를 지정하면 코호트별 생존 시간 좌표와 기증자(donor) 수를 살펴볼 수 있고, 두 코호트를 비교할 때는 카이제곱(chi-squared) 통계량까지 파악할 수 있습니다."
      },
      {
        "name": "gdc_top_mutated_cases_by_gene",
        "desc": "지정한 유전자(Ensembl ID, 예: \"ENSG00000141510\")의 변이에 가장 많이 영향을 받은 케이스를 확인할 수 있습니다. 순위화된 케이스와 데이터 카테고리·실험 전략별 관련 파일 수를 살펴볼 때 활용합니다."
      },
      {
        "name": "gdc_mutated_cases_count_by_project",
        "desc": "프로젝트별로 집계된 SSM 데이터 케이스 수를 확인할 수 있습니다. 어떤 프로젝트에 체세포 변이 데이터를 가진 케이스가 얼마나 있는지 파악할 때 활용합니다."
      },
      {
        "name": "gdc_rest_mapping",
        "desc": "GDC REST API에서 사용할 수 있는 필드 정보를 확인할 수 있습니다. 엔드포인트(예: \"files\", \"cases\")별로 사용 가능한 필드·타입·기본 필드·expand 그룹을 살펴볼 때 활용합니다."
      }
    ]
  },
  {
    "sys": "biomcp-mcp-server",
    "nameKo": "BioMCP",
    "desc": "PubMed·ClinicalTrials.gov·OpenFDA·MyGene/MyVariant/MyChem/MyDisease·ChEMBL를 통합한 생의학 지식 라우터입니다. 유전자·변이·질병·약물·임상시험·경로·논문에 걸친 정보를 한곳에서 교차 조회할 때 활용합니다.",
    "tools": [
      {
        "name": "search_genes",
        "desc": "심볼·이름·타입·염색체·경로(pathway)·GO term으로 유전자를 검색할 수 있습니다. 각 유전자의 심볼·이름·Entrez ID·유전체 좌표·UniProt ID·OMIM ID를 확인할 때 활용합니다."
      },
      {
        "name": "search_variants",
        "desc": "유전자·임상적 의의(clinical significance)·빈도·영향(consequence)으로 변이를 검색할 수 있습니다. 변이 ID·유전자·단백질 변화·ClinVar 의의·리뷰 별점·gnomAD AF·REVEL 점수를 확인할 때 활용합니다."
      },
      {
        "name": "search_articles",
        "desc": "유전자·질병·약물·키워드·저자로 생의학 문헌을 검색할 수 있습니다. PMID·제목·저널·연도·저자·초록 일부를 확인할 때 활용합니다."
      },
      {
        "name": "search_trials",
        "desc": "적응증·중재(intervention)·변이·적격성(eligibility) 텍스트로 임상시험을 검색할 수 있습니다. NCT ID·제목·상(phase)·상태·적응증·중재·스폰서를 확인할 때 활용합니다."
      },
      {
        "name": "search_drugs",
        "desc": "이름·표적 유전자·적응증·작용 기전(mechanism of action)으로 약물을 검색할 수 있습니다. 약물명·타입·표적 유전자·적응증·ATC 코드·승인 상태를 확인할 때 활용합니다."
      },
      {
        "name": "search_diseases",
        "desc": "이름·온톨로지 출처·유전 양식(inheritance pattern)·HPO 표현형으로 질병을 검색할 수 있습니다. 질병명·MONDO/DOID/MeSH ID·유전 양식·온톨로지 출처를 확인할 때 활용합니다."
      },
      {
        "name": "search_pathways",
        "desc": "이름이나 키워드로 생물학적 경로를 검색할 수 있습니다. 경로명·Reactome stable ID·종(species)·최상위 경로를 확인할 때 활용합니다."
      },
      {
        "name": "search_proteins",
        "desc": "이름·액세션(accession)·키워드로 단백질을 검색할 수 있습니다. UniProt 액세션·단백질명·유전자 심볼·생물체·리뷰 상태·서열 길이를 확인할 때 활용합니다."
      },
      {
        "name": "search_adverse_events",
        "desc": "특정 약물이나 의료기기의 시판 후(post-market) 이상사례 보고를 검색할 수 있습니다. 보고 ID·약물명·반응(reaction)·결과(outcome)·중증도·보고일을 확인할 때 활용합니다."
      },
      {
        "name": "search_gwas",
        "desc": "유전자·형질(trait)·영역(region)으로 전장유전체 연관분석(GWAS) 결과를 검색할 수 있습니다. SNP rsID·유전자·형질·p-value·오즈비(odds ratio)·연구 PMID·조상(ancestry)을 확인할 때 활용합니다."
      },
      {
        "name": "search_pgx",
        "desc": "유전자나 약물로 약물유전체(pharmacogenomic) 유전자-약물 상호작용을 검색할 수 있습니다. 유전자·약물·CPIC 등급·PGx 검사 권고·가이드라인명 및 URL을 확인할 때 활용합니다."
      },
      {
        "name": "search_phenotype",
        "desc": "일련의 HPO 표현형 term을 의미적 유사도(semantic similarity)로 질병에 매칭해 볼 수 있습니다. 표현형 프로파일로 후보 질병을 식별할 때 활용하며, 질병명·MONDO ID·매칭 점수·매칭된 HPO term을 확인할 수 있습니다."
      },
      {
        "name": "search_all",
        "desc": "유전자·변이·논문·임상시험·약물·질병·경로에 걸친 검색을 한 번에 수행할 수 있습니다. 미지의 주제를 탐색할 때 출발점으로 활용하며, 엔티티 타입별 건수와 상위 결과를 한눈에 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene",
        "desc": "심볼(예: \"BRAF\", \"TP53\")로 전체 유전자 레코드를 확인할 수 있습니다. 좌표·UniProt/OMIM ID·별칭은 물론, 경로·GO term·질병 연관·단백질 정보·약물 상호작용·CIViC 근거까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_variant",
        "desc": "rsID·HGVS·유전자+변화 표기(예: \"BRAF V600E\")로 전체 변이 주석을 확인할 수 있습니다. 기본 주석에 더해 병원성(pathogenicity) 예측·인구집단 AF·보존 점수(conservation score)·COSMIC/CIViC/cBioPortal 종양학 근거·GWAS 결과까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_article",
        "desc": "PMID·PMCID·DOI로 논문을 확인할 수 있습니다. 제목·저자·저널·초록은 물론, 언급된 유전자/변이/약물 등 엔티티 주석과 전문(오픈 액세스 한정)까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_trial",
        "desc": "NCT ID(예: \"NCT02576665\")로 임상시험 상세 정보를 확인할 수 있습니다. 제목·상·상태·적응증·스폰서·날짜는 물론, 적격성 기준·시험 기관 위치·결과 지표(outcome measure)·시험군(arm)·참고문헌까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_drug",
        "desc": "이름(예: \"pembrolizumab\", \"carboplatin\")으로 약물 레코드를 확인할 수 있습니다. 약물명·타입·승인 상태·작용 기전은 물론, FDA 라벨·공급 부족 상태·표적 유전자·적응증·약물 상호작용까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_disease",
        "desc": "이름(예: \"melanoma\")이나 MONDO ID로 질병 레코드를 확인할 수 있습니다. 질병명·MONDO ID·동의어·유전 양식은 물론, 연관 유전자·경로·표현형(HPO)·변이·동물 모델·유병률·CIViC 근거까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_pathway",
        "desc": "stable ID(예: \"R-HSA-5673001\")로 경로 레코드를 확인할 수 있습니다. 경로명·종·최상위 카테고리는 물론, 참여 유전자·반응 이벤트·강화(enrichment) 통계까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_protein",
        "desc": "UniProt 액세션(예: \"P15056\")이나 유전자 심볼로 단백질 레코드를 확인할 수 있습니다. 단백질명·유전자·기능 요약·서열 길이·리뷰 상태는 물론, 도메인 구조·단백질 상호작용·3D 구조(PDB/AlphaFold)까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_adverse_event",
        "desc": "FAERS safetyreportid나 MAUDE mdr_report_key로 개별 이상사례 보고를 확인할 수 있습니다. 보고 요약·의심 약물(suspect drug)·환자 인구통계는 물론, 반응·결과·병용 약물(concomitant medication)·임상 지침까지 살펴볼 수 있습니다."
      },
      {
        "name": "get_pgx",
        "desc": "유전자(예: \"CYP2D6\")나 약물(예: \"warfarin\")에 대한 전체 약물유전체 카드를 확인할 수 있습니다. CPIC 등급을 포함한 유전자-약물 상호작용은 물론, 용량 권고·대립유전자 빈도·임상 가이드라인·문헌 주석까지 살펴볼 수 있습니다."
      },
      {
        "name": "enrich_genes",
        "desc": "유전자 집합에 대해 GO·KEGG·Reactome 데이터베이스에 걸친 강화 분석(gene set enrichment analysis)을 수행할 수 있습니다. 유전자 목록에서 공통된 생물학적 기능이나 경로를 식별할 때 활용하며, 유의성이 높은 강화 term·p-value·유전자 수·출처 데이터베이스를 확인할 수 있습니다."
      },
      {
        "name": "article_entities",
        "desc": "PubMed 논문에서 NLP로 주석된 생의학 엔티티(유전자·변이·약물·질병)를 확인할 수 있습니다. 논문을 직접 읽지 않고도 어떤 엔티티를 다루는지 파악할 때 활용하며, 엔티티 텍스트·타입·정규화된 ID·언급 위치(offset)를 살펴볼 수 있습니다."
      },
      {
        "name": "batch_get",
        "desc": "동일한 타입의 여러 엔티티(유전자·변이·논문·임상시험·약물·질병 등)를 한 번에 조회할 수 있습니다. 여러 ID에 대한 전체 레코드를 한꺼번에 확인할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "gwas-catalog-mcp",
    "nameKo": "GWAS Catalog",
    "desc": "전장유전체 연관분석(GWAS) 결과를 확인할 수 있습니다. GWAS Catalog에서 제공하는 SNP-형질 연관성(p-value·효과 크기 포함)과 연구 메타데이터를 조회할 때 활용합니다.",
    "tools": [
      {
        "name": "get_study",
        "desc": "GCST 연구 ID(예: 'GCST000001')로 GWAS 연구 메타데이터를 확인할 수 있습니다. 제목·제1저자·게재일·저널·PMID·조상/샘플 설명·코호트 규모·연결된 EFO 형질을 살펴볼 수 있습니다."
      },
      {
        "name": "get_association",
        "desc": "연관성 ID로 단일 GWAS 연관성 레코드를 확인할 수 있습니다. rsID·염색체:위치·p-value·전장유전체 유의 여부·효과 크기(beta 또는 오즈비)·신뢰구간·위험 대립유전자 빈도(risk allele frequency)·보고된 형질·연구 맥락을 살펴볼 수 있습니다."
      },
      {
        "name": "get_variant",
        "desc": "rsID(예: 'rs7903146')로 SNP 상세 정보를 확인할 수 있습니다. 염색체·염기쌍 위치(GRCh38)·기능적 분류·병합(merged) 상태·인접/매핑된 유전자 맥락을 살펴볼 수 있습니다."
      },
      {
        "name": "get_trait",
        "desc": "EFO ID(예: 'EFO_0000305')로 EFO 형질 상세 정보를 확인할 수 있습니다. 형질 라벨·EFO URI·단축형 ID·GWAS Catalog 메타데이터를 살펴볼 수 있습니다."
      },
      {
        "name": "search_variants_in_region",
        "desc": "GRCh38 유전체 영역 내의 GWAS 연관성을 확인할 수 있습니다. 염색체와 시작·끝 위치를 지정하면 해당 구간의 rsID·p-value·전장유전체 유의 여부·효과 크기(beta/OR)·위험 대립유전자·매핑된 유전자·형질을 살펴볼 수 있고, 특정 EFO 형질로 범위를 좁힐 수도 있습니다."
      },
      {
        "name": "get_variants_from_efo_ids",
        "desc": "여러 EFO 형질 ID(예: ['EFO_0000305', 'EFO_0001360'])에 대한 GWAS 연관성을 한 번에 확인할 수 있습니다. 형질별로 p-value·효과 크기·변이·전장유전체 유의 여부를 살펴볼 수 있습니다."
      },
      {
        "name": "trait_variant_ranking",
        "desc": "특정 EFO 형질(예: 'EFO_0000305')의 GWAS 연관성을 p-value가 작은 순으로 순위화하여 확인할 수 있습니다. 상위 연관성의 rsID·p-value·전장유전체 유의 여부·효과 크기·매핑된 유전자를 살펴볼 수 있습니다."
      },
      {
        "name": "get_study_associations",
        "desc": "GCST 연구 ID(예: 'GCST000001')로 해당 연구의 모든 GWAS 연관성을 확인할 수 있습니다. rsID·p-value·전장유전체 유의 여부·효과 크기(beta/OR)·신뢰구간·위험 대립유전자·매핑된 유전자·보고된 형질을 살펴볼 수 있습니다."
      },
      {
        "name": "get_trait_studies",
        "desc": "EFO 형질 ID(예: 'EFO_0000305')에 연결된 모든 GWAS 연구를 확인할 수 있습니다. GCST 연구 ID·제목·제1저자·게재일·저널·PMID·조상/샘플 정보·코호트 규모를 살펴볼 수 있습니다."
      },
      {
        "name": "get_trait_associations",
        "desc": "EFO 형질 ID(예: 'EFO_0000305')에 대한 모든 GWAS 연관성을 확인할 수 있습니다. rsID·p-value·전장유전체 유의 여부·효과 크기(beta/OR)·신뢰구간·위험 대립유전자 빈도·매핑된 유전자·연구 맥락을 살펴볼 수 있습니다."
      },
      {
        "name": "get_region_trait_associations",
        "desc": "특정 EFO 형질에 대해 유전체 영역 내의 GWAS 요약 통계(summary statistics) 연관성을 확인할 수 있습니다. 효과 수준의 p-value·beta·표준오차(standard error)·효과 대립유전자 빈도(effect allele frequency)·전장유전체 유의 여부를 살펴볼 수 있습니다."
      },
      {
        "name": "get_associations_from_variant",
        "desc": "특정 rsID(예: 'rs7903146')에 대해 모든 형질에 걸친 GWAS 연관성을 확인할 수 있습니다. 형질명·EFO ID·p-value·전장유전체 유의 여부·효과 크기(beta/OR)·신뢰구간·위험 대립유전자·매핑된 유전자·연구 맥락을 살펴볼 수 있습니다."
      }
    ]
  },
  {
    "sys": "cellosaurus-mcp-server",
    "nameKo": "Cellosaurus",
    "desc": "세포주(cell line)의 식별 정보와 메타데이터를 확인할 수 있습니다. 질병 기원·조직·종·성별·연령·동의어·STR 프로파일·계통(lineage)을 조회할 수 있습니다.",
    "tools": [
      {
        "name": "search_cell_lines",
        "desc": "Solr 쿼리 문법으로 세포주를 검색할 수 있습니다. 이름·액세션·동의어·카테고리·질병·종·조직 기원·성별·연령·모(parent) 세포주를 확인할 수 있습니다. 쿼리 예시: 이름 \"id:HeLa\"·\"sy:HeLa\", 종 \"ox:human\"·\"ox:9606\", 질병 \"di:hepatoblastoma\", 조직 \"derived-from-site:liver\", 조합 \"ox:human di:cancer\"."
      },
      {
        "name": "get_cell_line_info",
        "desc": "Cellosaurus 액세션 번호(예: HeLa의 CVCL_0030, MCF7의 CVCL_0004)로 특정 세포주의 상세 정보를 조회할 수 있습니다. 핵심 메타데이터에 더해 코멘트·웹 페이지·서열 변이·핵형(karyotype)·MSI 상태를 살펴볼 수 있습니다."
      },
      {
        "name": "get_release_info",
        "desc": "현재 데이터베이스 릴리스 정보를 확인할 수 있습니다. 버전 번호·릴리스 날짜·세포주 수 통계를 조회할 수 있습니다."
      },
      {
        "name": "find_cell_lines_by_disease",
        "desc": "특정 임상 질병 또는 암 유형 환자에서 유래한 세포주를 찾을 수 있습니다. NCIt(미국 국립암연구소 시소러스) 용어 기반의 질병 필드를 검색하여 액세션·이름·질병·종·조직 기원을 확인할 수 있습니다. 질병/병태명(예: Breast carcinoma·Melanoma·Glioblastoma·hepatoblastoma·colorectal adenocarcinoma·acute lymphoblastic leukemia)으로 조회할 때 활용합니다."
      },
      {
        "name": "find_cell_lines_by_tissue",
        "desc": "특정 조직 또는 장기(예: liver·lung·breast·brain·colon·kidney)에서 유래한 세포주를 찾을 수 있습니다. 액세션·이름·조직 기원·종·세포 유형(cell type)을 확인할 수 있습니다."
      },
      {
        "name": "list_available_fields",
        "desc": "세포주 검색과 상세 조회에서 사용할 수 있는 모든 필드명을 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "ema-mcp-server",
    "nameKo": "EMA",
    "desc": "유럽의약품청(EMA)의 규제 기록을 확인할 수 있습니다. EU 시판허가·희귀의약품 지정(orphan designation)·공급 부족·안전성 검토 의뢰(referral)·EPAR 문서를 조회할 수 있습니다.",
    "tools": [
      {
        "name": "search_medicines",
        "desc": "활성 성분·치료 영역·규제 플래그로 EU 허가 의약품을 검색할 수 있습니다. 의약품명·활성 성분·상태·허가일·치료 영역·orphan/PRIME/바이오시밀러 플래그를 확인할 수 있습니다."
      },
      {
        "name": "get_medicine_by_name",
        "desc": "상품명(trade name)으로 단일 의약품의 전체 레코드를 조회할 수 있습니다. 활성 성분·상태·허가일·치료 적응증·EMA 제품 페이지 URL을 확인할 수 있습니다."
      },
      {
        "name": "get_orphan_designations",
        "desc": "EU 희귀의약품 지정 내역을 확인할 수 있습니다. 지정 번호·활성 성분·의도된 용도(병태)·상태·지정일을 조회할 수 있습니다."
      },
      {
        "name": "get_supply_shortages",
        "desc": "EU 의약품 공급 부족 통지 내역을 확인할 수 있습니다. 의약품명·활성 성분·부족 상태·치료 영역·부족 기간을 조회할 수 있습니다."
      },
      {
        "name": "get_referrals",
        "desc": "EU 전역 안전성 검토 의뢰(Article 20, 31, 107i 절차) 내역을 확인할 수 있습니다. 절차명·활성 성분·안전성 의뢰 플래그·시작일·현재 상태를 조회할 수 있습니다."
      },
      {
        "name": "get_post_auth_procedures",
        "desc": "허가 후(post-authorisation) 절차(라벨 갱신·Type I/II 변경·갱신) 내역을 확인할 수 있습니다. 의약품명·절차 번호·절차 유형·시작/종료일·결과를 조회할 수 있습니다."
      },
      {
        "name": "get_dhpcs",
        "desc": "의료전문가 대상 직접 통보문(Direct Healthcare Professional Communications, DHPC) — 처방자에게 발송되는 긴급 안전성 서한 — 내역을 확인할 수 있습니다. 의약품명·활성 성분·DHPC 유형·배포일·링크를 조회할 수 있습니다."
      },
      {
        "name": "get_psusas",
        "desc": "정기적 안전성 정보 단일 평가(Periodic Safety Update Single Assessments, PSUSA) — 정기 유익성-위해성 검토 — 내역을 확인할 수 있습니다. 활성 성분·절차 번호·규제 결과·평가일을 조회할 수 있습니다."
      },
      {
        "name": "get_pips",
        "desc": "소아 연구 계획(Paediatric Investigation Plans, PIP) — 소아용 의약품에 필수적인 계획 — 내역을 확인할 수 있습니다. 활성 성분·치료 영역·결정 유형·결정일·준수(compliance) 상태를 조회할 수 있습니다."
      },
      {
        "name": "get_herbal_medicines",
        "desc": "약용식물제제위원회(Committee on Herbal Medicinal Products, HMPC)의 생약(herbal medicine) 평가 내역을 확인할 수 있습니다. 식물 학명·일반명·치료 영역·평가 상태를 조회할 수 있습니다."
      },
      {
        "name": "get_article58_medicines",
        "desc": "Article 58(주로 개발도상국 등 EU 외 사용 목적)에 따라 평가된 의약품 내역을 확인할 수 있습니다. 의약품명·활성 성분·치료 영역·상태를 조회할 수 있습니다."
      },
      {
        "name": "search_epar_documents",
        "desc": "유럽 공공평가보고서(European Public Assessment Report, EPAR) 문서를 검색할 수 있습니다. 문서명·의약품·문서 유형·언어·게시일·다운로드 URL을 확인할 수 있습니다."
      },
      {
        "name": "search_all_documents",
        "desc": "모든 EMA 규제 문서(EPAR·가이드라인·과학적 자문 등)를 검색할 수 있습니다. 문서 제목·유형·카테고리·게시일·URL을 확인할 수 있습니다."
      },
      {
        "name": "search_non_epar_documents",
        "desc": "EPAR 이외의 EMA 문서(가이드라인·회의 보고서·과학적 자문 등)를 검색할 수 있습니다. 문서 제목·유형·게시일·URL을 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "alphafold-server",
    "nameKo": "AlphaFold",
    "desc": "AlphaFold 단백질 구조 데이터베이스에서 DeepMind/EMBL-EBI가 AI로 예측한 3D 단백질 구조를 확인할 수 있습니다. UniProt accession으로 구조 예측을 조회하고, 잔기별 신뢰도 점수(pLDDT)를 확인하며, 구조 메타데이터를 일괄 조회할 수 있습니다. 2억 개 이상의 단백질 구조를 포함하며, 실험적으로 결정된 구조는 PDB를 활용하십시오.",
    "tools": [
      {
        "name": "get_structure",
        "desc": "UniProt accession에 대한 AlphaFold 구조 메타데이터를 확인할 수 있습니다. 엔트리 ID·유전자·생물종·taxon ID·서열 길이·예측 영역 커버리지·카테고리별 pLDDT 비율(very-high/confident/low/very-low)·전체 pLDDT 점수·모델 버전·생성일과 PDB/CIF/BCIF/PAE 파일 다운로드 URL을 살펴볼 수 있습니다."
      },
      {
        "name": "check_availability",
        "desc": "주어진 UniProt accession에 대해 AlphaFold 구조 예측이 존재하는지 확인할 수 있습니다. 예측 존재 여부(available)·발견된 isoform 엔트리 수·최신 모델 버전·생성일을 조회할 수 있습니다."
      },
      {
        "name": "get_organism_stats",
        "desc": "UniProt accession의 모든 isoform에 대한 pLDDT 품질 통계를 확인할 수 있습니다. isoform별 fractionPlddtVeryHigh·fractionPlddtConfident·fractionPlddtLow·fractionPlddtVeryLow·globalMetricValue 세부 내역과 집계 평균을 살펴볼 수 있어, 예측 품질이 isoform마다 다른 다중 isoform 단백질을 파악할 때 활용합니다."
      },
      {
        "name": "get_confidence_scores",
        "desc": "AlphaFold 예측의 잔기별 pLDDT 신뢰도 점수를 확인할 수 있습니다. 전체 잔기 수·카테고리별 개수·평균 요약과 점수 기준 상위/하위 잔기를 살펴볼 수 있으며, 특정 신뢰도 컷오프(0~100) 이상의 잔기로 범위를 좁혀 확인할 수 있습니다."
      },
      {
        "name": "analyze_confidence_regions",
        "desc": "AlphaFold 예측에서 연속된 고신뢰/저신뢰 영역을 확인할 수 있습니다. 카테고리 분포(very-high ≥90·confident 70~90·low 50~70·very-low <50)와 very-high(≥90)·very-low(<50) 잔기의 연속 구간을 시작/종료 위치 및 평균 점수와 함께 살펴볼 수 있어, 무질서 영역이나 구조적 코어를 짚어낼 때 활용합니다."
      },
      {
        "name": "get_prediction_metadata",
        "desc": "AlphaFold 엔트리의 예측 메타데이터를 확인할 수 있습니다. 엔트리 ID·UniProt accession·모델 생성일·최신 버전·사용 가능한 모든 버전 번호·생물종·서열 길이·예측 영역(시작/종료 위치)·커버리지 비율과 구조 다운로드 URL(PDB·CIF·BCIF·PAE 이미지·PAE JSON)을 살펴볼 수 있습니다."
      },
      {
        "name": "batch_structure_info",
        "desc": "여러 UniProt accession에 대한 AlphaFold 구조 메타데이터를 한 번에 확인할 수 있습니다. 각 accession의 엔트리 ID·유전자·설명·생물종·taxon ID·서열 길이·모델 버전·생성일·pLDDT 비율·전체 메트릭 값·다운로드 URL(PDB·CIF·PAE)을 살펴볼 수 있습니다."
      },
      {
        "name": "batch_confidence_analysis",
        "desc": "여러 UniProt accession에 대한 pLDDT 신뢰도 지표를 한 번에 확인할 수 있습니다. 각 accession의 유전자·서열 길이·전체 pLDDT 점수·카테고리별 pLDDT 비율과 도출된 품질 카테고리(very-high/confident/low/very-low)를 살펴볼 수 있습니다."
      },
      {
        "name": "get_structures_summary",
        "desc": "여러 UniProt accession에 대한 AlphaFold 예측을 나란히 비교할 수 있습니다. 각 엔트리의 유전자·설명·생물종·서열 길이·커버리지 비율·전체 pLDDT 점수·품질 카테고리·모델 버전·생성일·PDB 다운로드 URL을 표 형태로 살펴볼 수 있습니다."
      },
      {
        "name": "get_coverage_info",
        "desc": "AlphaFold 예측의 서열 커버리지 정보를 확인할 수 있습니다. 전체 서열 길이·예측 영역(시작/종료 잔기 위치)·예측 영역 길이·커버리지 비율·커버리지 완전 여부·미포함 N-말단 및 C-말단 잔기 수를 살펴볼 수 있어, 긴 단백질이나 본질적 무질서 단백질의 불완전한 커버리지를 파악할 때 활용합니다."
      },
      {
        "name": "validate_structure_quality",
        "desc": "AlphaFold 예측의 전반적 신뢰성을 확인할 수 있습니다. 품질 카테고리(very-high/confident/low/very-low)·전체 pLDDT 점수·서열 커버리지 비율·카테고리별 pLDDT 비율과 품질 경고 목록(예: 불완전 커버리지·저신뢰 잔기 비율 과다)을 살펴볼 수 있어, 구조를 도킹이나 기능 주석에 사용하기 전에 활용합니다."
      },
      {
        "name": "export_for_pymol",
        "desc": "AlphaFold 구조를 PyMOL에서 불러와 시각화할 수 있는 스크립트를 확인할 수 있습니다. PDB 다운로드 URL과 fetch/cartoon/coloring 명령이 포함된 스크립트, 사용 안내를 살펴볼 수 있으며, pLDDT 기반 색상 지정(very-high=파랑·confident=청록·low=노랑·very-low=주황)을 포함할 수 있습니다."
      },
      {
        "name": "export_for_chimerax",
        "desc": "AlphaFold 구조를 ChimeraX에서 불러와 시각화할 수 있는 스크립트를 확인할 수 있습니다. PDB 다운로드 URL과 ChimeraX 명령 스크립트, 사용 안내를 살펴볼 수 있으며, pLDDT 신뢰도 기반 색상 지정을 포함할 수 있습니다."
      },
      {
        "name": "get_api_status",
        "desc": "AlphaFold EBI API에 접근 가능한지 확인할 수 있습니다. status(ONLINE/OFFLINE)·지연 시간(ms)·프로브 결과를 살펴볼 수 있어, 일괄 작업 전 연결 상태를 점검할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "uniprot-server",
    "nameKo": "UniProt",
    "desc": "EMBL-EBI·SIB·PIR가 제공하는 종합 단백질 지식베이스 UniProt에서 단백질의 기능·세포 내 위치·번역 후 수식(PTM)·질병 연관성·GO terms와 100개 이상 데이터베이스와의 상호 참조를 확인할 수 있습니다. UniProtKB/Swiss-Prot(검수) 엔트리는 P00533 같은 accession을, TrEMBL(미검수)은 A0A... 형식을 사용합니다.",
    "tools": [
      {
        "name": "search_proteins",
        "desc": "자유 텍스트 키워드·단백질명·고급 쿼리 구문(예: \"kinase AND reviewed:true\")으로 UniProtKB의 단백질을 검색할 수 있습니다. accession·단백질명·유전자명·생물종·주석 점수·단백질 존재 수준(protein existence)을 확인할 수 있습니다."
      },
      {
        "name": "get_protein_info",
        "desc": "accession으로 특정 UniProt 단백질 엔트리의 종합 정보를 확인할 수 있습니다. 단백질명·유전자명·생물종·주석 점수·단백질 존재 증거·서열·분자량·세포 내 위치·기능·질병 연관성·키워드·feature 요약을 살펴볼 수 있습니다(예: 인간 TP53의 P04637)."
      },
      {
        "name": "search_by_gene",
        "desc": "특정 유전자가 암호화하는 단백질을 UniProtKB에서 조회할 수 있습니다. accession·단백질명·유전자명·생물종·주석 점수·단백질 존재를 확인할 수 있습니다."
      },
      {
        "name": "get_protein_sequence",
        "desc": "UniProt 단백질의 아미노산 서열을 조회할 수 있습니다. accession·단백질명·유전자명·생물종과 함께 서열 문자열·길이·분자량을 확인할 수 있어 BLAST·정렬·모티프 검색 등 후속 분석에 활용합니다."
      },
      {
        "name": "get_protein_features",
        "desc": "신호 펩타이드·프로펩타이드·성숙 사슬(mature chain)·이황화 결합·자연 변이·돌연변이 유발(mutagenesis) 부위 등 UniProt 단백질의 주석된 서열 feature를 확인할 수 있습니다. feature 유형·서열 내 시작/종료 위치·설명·상호 참조(예: 변이에 대한 dbSNP ID)를 살펴볼 수 있습니다."
      },
      {
        "name": "compare_proteins",
        "desc": "여러 UniProt 단백질을 나란히 비교할 수 있습니다. 엔트리별 accession·엔트리명·생물종·서열 길이·분자량·전체 feature 개수·도메인 개수를 표로 확인할 수 있어 파라로그·isoform·직교 단백질 간 차이를 빠르게 파악할 때 활용합니다."
      },
      {
        "name": "get_protein_homologs",
        "desc": "쿼리 단백질과 상동인(같은 종 또는 다른 종) 단백질을 UniProtKB에서 찾을 수 있습니다. 유사한 단백질명을 가진 엔트리를 살펴볼 수 있습니다."
      },
      {
        "name": "get_protein_orthologs",
        "desc": "동일한 유전자명을 가진 다른 종의 직교(ortholog) 단백질을 UniProtKB에서 찾을 수 있습니다."
      },
      {
        "name": "get_phylogenetic_info",
        "desc": "UniProt 단백질의 분류학적 계통(taxonomic lineage)과 진화적 맥락을 확인할 수 있습니다. 전체 분류 계통(계 → 종)·생물종 메타데이터·주석된 진화적 기원 또는 계통 범위 코멘트를 살펴볼 수 있습니다."
      },
      {
        "name": "get_protein_structure",
        "desc": "UniProt 단백질에 대한 PDB 구조 참조를 조회할 수 있습니다. 방법(X-ray·NMR·EM)·해상도·사슬 커버리지를 포함한 PDB ID와 서브유닛 구성 주석을 확인할 수 있습니다."
      },
      {
        "name": "get_protein_domains_detailed",
        "desc": "UniProt 단백질의 상세 도메인 및 기능 영역 주석을 확인할 수 있습니다. 도메인 경계·반복 영역과 InterPro·Pfam·SMART 상호 참조를 살펴볼 수 있어 단백질의 모듈형 구조를 파악할 때 활용합니다."
      },
      {
        "name": "get_protein_variants",
        "desc": "UniProt 단백질의 자연 변이·질병 연관 돌연변이·돌연변이 유발 주석을 확인할 수 있습니다. 서열 위치·원래 및 대체 잔기·변이 설명·dbSNP ID를 살펴볼 수 있으며 생식세포(germline) 변이는 별도로 확인할 수 있습니다."
      },
      {
        "name": "analyze_sequence_composition",
        "desc": "UniProt 단백질의 아미노산 조성 통계를 확인할 수 있습니다. 서열 길이·분자량·잔기별 개수 및 빈도·소수성·하전·극성 잔기 그룹의 집계를 살펴볼 수 있어 빠른 생화학적 지문(fingerprint)으로 활용합니다."
      },
      {
        "name": "get_protein_pathways",
        "desc": "UniProt 단백질의 경로(pathway) 연관성을 확인할 수 있습니다. KEGG·Reactome 상호 참조와 UniProt pathway/기능 코멘트를 살펴볼 수 있어 단백질을 생물학적 맥락에 위치시킬 때 활용합니다."
      },
      {
        "name": "get_protein_interactions",
        "desc": "UniProt 엔트리의 단백질-단백질 상호작용을 확인할 수 있습니다. STRING·IntAct 상호 참조 ID, 실험적으로 문서화된 이진(binary) 상호작용(파트너 accession·유전자명·실험 횟수), 서브유닛 구성 주석을 살펴볼 수 있습니다."
      },
      {
        "name": "search_by_function",
        "desc": "GO term 또는 기능 키워드로 UniProtKB의 단백질을 검색할 수 있습니다."
      },
      {
        "name": "search_by_localization",
        "desc": "특정 세포 내 위치(subcellular localization)를 갖는 단백질을 UniProtKB에서 검색할 수 있습니다."
      },
      {
        "name": "batch_protein_lookup",
        "desc": "여러 UniProt accession에 대한 전체 단백질 정보를 한 번에 확인할 수 있습니다. accession별로 get_protein_info와 동일한 필드를 살펴볼 수 있습니다."
      },
      {
        "name": "advanced_search",
        "desc": "텍스트 쿼리·생물종·서열 길이 범위·분자량 범위·UniProt 키워드 태그 등 복합 필터로 UniProtKB를 검색할 수 있습니다. 모든 필터는 AND로 결합됩니다."
      },
      {
        "name": "search_by_taxonomy",
        "desc": "NCBI taxonomy ID 또는 이름으로 특정 분류군의 모든 단백질을 UniProtKB에서 검색할 수 있습니다."
      },
      {
        "name": "get_external_references",
        "desc": "UniProt 단백질 엔트리에서 외부 데이터베이스로의 상호 참조를 확인할 수 있습니다. Ensembl(전사체/유전자 ID)·RefSeq(단백질/뉴클레오타이드 ID)·EMBL(코딩 서열)·HGNC(인간 유전자 명명법)·KEGG(경로 ID)·DrugBank(약물 표적 ID)를 살펴볼 수 있습니다."
      },
      {
        "name": "get_literature_references",
        "desc": "UniProt 단백질 엔트리의 실험적 증거를 제공하는 PubMed 연계 문헌을 확인할 수 있습니다. 참조별 PubMed ID·제목·출판 연도·증거 범위(예: \"FUNCTION\", \"STRUCTURE\", \"MUTAGENESIS\")를 살펴볼 수 있습니다."
      },
      {
        "name": "get_annotation_confidence",
        "desc": "UniProt 엔트리의 신뢰성과 주석 품질을 확인할 수 있습니다. 검수 상태(Reviewed = Swiss-Prot 큐레이션, Unreviewed = TrEMBL 자동)·주석 점수(1~5 별점)·단백질 존재 증거 수준·지원 문헌 참조 개수를 살펴볼 수 있습니다."
      },
      {
        "name": "export_protein_data",
        "desc": "UniProt 단백질 엔트리를 특수 형식으로 내보낼 수 있습니다. 게놈 feature 주석은 \"gff\", 뉴클레오타이드 서열 주석은 \"genbank\" 또는 \"embl\", 완전한 UniProt XML은 \"xml\" 형식으로 확인할 수 있습니다."
      },
      {
        "name": "validate_accession",
        "desc": "UniProt accession이 유효하며 UniProtKB에 존재하는지 확인할 수 있습니다. 유효성·존재 여부·엔트리 유형·기본(primary) accession을 살펴볼 수 있습니다."
      },
      {
        "name": "get_taxonomy_info",
        "desc": "UniProt 단백질의 생물종에 대한 전체 분류학적 분류와 계통을 확인할 수 있습니다. NCBI taxonomy ID·학명·일반명과 가장 일반적인 분류군부터 가장 구체적인 분류군까지의 완전한 계통을 살펴볼 수 있습니다."
      }
    ]
  },
  {
    "sys": "bioontology-server",
    "nameKo": "BioOntology",
    "desc": "900개 이상의 생물의학 온톨로지 저장소인 BioPortal에서 온톨로지 용어를 검색하고, 온톨로지를 탐색·브라우징하며, 자유 텍스트를 온톨로지 개념으로 주석(annotation)하고, 주제에 맞는 온톨로지를 추천받으며, 클래스 계층 구조와 사용 분석(analytics)을 확인할 수 있습니다. GO·MESH·NCIT·SNOMED·ICD·UMLS 등 다수를 지원합니다.",
    "tools": [
      {
        "name": "search_terms",
        "desc": "900개 이상의 생물의학 온톨로지에 걸쳐 키워드로 온톨로지 클래스/용어를 검색할 수 있습니다. 일치하는 클래스 ID·선호 레이블·출처 온톨로지 약어·동의어·정의·매치 유형을 확인할 수 있으며, 특정 온톨로지(예: \"GO,MESH,NCIT\")로 제한하거나 UMLS 의미 유형·CUI 코드로 필터링할 수 있습니다."
      },
      {
        "name": "search_properties",
        "desc": "BioPortal 온톨로지에서 레이블로 온톨로지 속성(object·annotation·datatype)을 검색할 수 있습니다. 속성 ID·레이블·정의·속성 유형·출처 온톨로지를 확인할 수 있어 OWL/OBO 추론이나 SPARQL 쿼리에 필요한 속성 URI를 찾을 때 활용합니다."
      },
      {
        "name": "search_ontologies",
        "desc": "BioPortal에 등록된 900개 이상의 온톨로지를 이름 또는 약어로 브라우징하거나 필터링할 수 있습니다. 온톨로지 약어·전체 이름·유형을 확인할 수 있습니다."
      },
      {
        "name": "get_ontology_info",
        "desc": "약어로 특정 BioPortal 온톨로지의 메타데이터를 확인할 수 있습니다. 전체 이름·온톨로지 유형(OWL/OBO/UMLS/VALUE_SET)·관리 기관·도메인 태그·그룹 소속을 살펴볼 수 있습니다."
      },
      {
        "name": "annotate_text",
        "desc": "자유 텍스트 형식의 생물의학 콘텐츠를 BioPortal의 일치하는 온톨로지 용어로 주석할 수 있습니다. 일치한 클래스 ID·선호 레이블·출처 온톨로지 약어·일치한 문자 범위(span)를 확인할 수 있어 임상 노트·논문 초록·병리 보고서의 NLP 개념 추출에 활용합니다."
      },
      {
        "name": "recommend_ontologies",
        "desc": "주어진 텍스트나 키워드 목록에 가장 적합한 BioPortal 온톨로지를 추천받을 수 있습니다. 전체 평가 점수와 세부 점수(커버리지·전문성·수용도·상세도)로 순위가 매겨진 온톨로지를 확인할 수 있어 용어 검색이나 텍스트 주석 전에 관련성 높은 온톨로지를 찾을 때 활용합니다."
      },
      {
        "name": "batch_annotate",
        "desc": "여러 텍스트를 한 번에 주석하여 각각 일치한 온톨로지 용어 목록을 확인할 수 있습니다. 결과별 클래스 ID·선호 레이블·출처 온톨로지 약어·일치한 문자 범위를 살펴볼 수 있습니다."
      },
      {
        "name": "get_class_info",
        "desc": "URI로 특정 온톨로지 클래스의 상세 정보를 확인할 수 있습니다. 선호 레이블·동의어·정의·의미 유형·CUI 코드·표기(notation)·계층 위치(상위/하위)를 살펴볼 수 있습니다."
      },
      {
        "name": "get_ontology_metrics",
        "desc": "특정 BioPortal 온톨로지의 규모 및 품질 지표를 확인할 수 있습니다. 전체 클래스 수·속성 수·매핑 수·개체(individual) 수·주석 지표를 살펴볼 수 있어 주석이나 용어 검색에 사용할 온톨로지의 커버리지와 규모를 평가할 때 활용합니다."
      },
      {
        "name": "get_analytics_data",
        "desc": "BioPortal 전체 또는 특정 온톨로지의 방문자 통계와 사용 추세를 확인할 수 있습니다. 월별 페이지뷰 및 방문 횟수를 살펴볼 수 있습니다(2013년부터 데이터 제공)."
      }
    ]
  },
  {
    "sys": "biothings-mcp-server",
    "nameKo": "BioThings",
    "desc": "BioThings.io API(MyGene.info + MyVariant.info) — 유전자·변이 주석 통합기. 유전자 주석에서 Entrez·Ensembl·UniProt·GO terms·KEGG·경로(pathway) 상호 참조를, 변이 주석에서 ClinVar·gnomAD·dbSNP·CADD·SIFT·PolyPhen 점수를 확인할 수 있습니다.",
    "tools": [
      {
        "name": "get_gene_annotation",
        "desc": "Entrez ID 또는 Ensembl 유전자 ID로 MyGene.info에서 유전자의 완전한 주석 레코드를 확인할 수 있습니다. GO terms·KEGG/Reactome 경로·UniProt 상호 참조·발현 데이터·게놈 좌표를 살펴볼 수 있습니다."
      },
      {
        "name": "query_genes",
        "desc": "Elasticsearch 쿼리 구문으로 MyGene.info 유전자를 검색할 수 있습니다. 필드 범위 쿼리(예: \"symbol:TP53\")·텍스트 검색(예: \"summary:insulin\")·게놈 구간(예: \"chr1:1000-2000\")으로 유전자를 조회할 수 있습니다."
      },
      {
        "name": "get_variant_annotation",
        "desc": "HGVS ID로 MyVariant.info에서 특정 변이의 완전한 주석 레코드를 확인할 수 있습니다. ClinVar 임상적 의의 및 질환·gnomAD/ExAC 대립유전자 빈도·CADD 유해성 점수·SIFT 및 PolyPhen2 병원성 예측·dbSNP rsID·hg19/hg38 좌표를 살펴볼 수 있습니다."
      },
      {
        "name": "query_variants",
        "desc": "Elasticsearch 쿼리 구문으로 MyVariant.info 변이를 검색할 수 있습니다. rsID 조회(예: \"rs58991260\")·게놈 범위 쿼리(예: \"chr1:69000-70000\")·필드 범위 쿼리(예: \"dbnsfp.genename:CDK2\")로 chr·rsID·유전자·CADD 점수·ClinVar 의의·SIFT/PolyPhen을 확인할 수 있습니다."
      },
      {
        "name": "batch_gene_query",
        "desc": "Entrez 또는 Ensembl ID 목록으로 여러 유전자의 주석을 한 번에 확인할 수 있습니다. get_gene_annotation과 동일한 정보를 일괄로 살펴볼 수 있습니다."
      },
      {
        "name": "batch_variant_query",
        "desc": "HGVS 변이 ID 목록으로 여러 변이의 주석을 한 번에 확인할 수 있습니다. get_variant_annotation과 동일한 정보를 일괄로 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_metadata",
        "desc": "MyGene.info API 메타데이터를 확인할 수 있습니다. 사용 가능한 데이터 출처(Entrez·Ensembl·UniProt·KEGG·Reactome 등)·데이터베이스 빌드 날짜·전체 유전자 수·분류군 커버리지를 살펴볼 수 있습니다."
      },
      {
        "name": "get_variant_metadata",
        "desc": "MyVariant.info API 메타데이터를 확인할 수 있습니다. 사용 가능한 데이터 출처(ClinVar·gnomAD·dbSNP·CADD·SIFT·PolyPhen2 등)·게놈 어셈블리 버전(hg19/hg38)·빌드 날짜를 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_fields",
        "desc": "MyGene.info 유전자 레코드에서 사용 가능한 모든 주석 필드를 설명 및 데이터 유형과 함께 확인할 수 있습니다. 필드명(예: \"go.BP\", \"pathway.kegg.name\", \"uniprot.Swiss-Prot\")을 찾을 때 활용합니다."
      },
      {
        "name": "get_variant_fields",
        "desc": "MyVariant.info 변이 레코드에서 사용 가능한 모든 주석 필드를 설명 및 데이터 유형과 함께 확인할 수 있습니다. 필드명(예: \"clinvar.rcv.clinical_significance\", \"cadd.phred\", \"dbnsfp.sift.pred\")을 찾을 때 활용합니다."
      },
      {
        "name": "search_genes_by_pathway",
        "desc": "KEGG·Reactome·BioCarta·PID·WikiPathways·NetPath의 특정 생물학적 경로에 주석된 유전자를 찾을 수 있습니다. 경로 이름 또는 ID(예: \"cell cycle\", \"hsa04110\", \"p53 signaling\")로 유전자 심볼·이름·경로 주석을 확인할 수 있습니다."
      },
      {
        "name": "search_genes_by_go_term",
        "desc": "생물학적 과정(BP)·분자 기능(MF)·세포 구성요소(CC)에 걸쳐 특정 Gene Ontology(GO) term으로 주석된 유전자를 찾을 수 있습니다. GO term 이름(예: \"apoptosis\") 또는 GO ID(예: \"GO:0006915\")로 조회할 수 있으며, GO ID는 해당 정확한 term으로 직접 주석된 유전자만, term 이름은 더 넓은 결과를 확인할 수 있습니다."
      },
      {
        "name": "search_variants_by_gene",
        "desc": "HGNC 심볼(예: \"BRCA1\", \"TP53\")로 특정 유전자 내부 또는 인근의 변이를 찾을 수 있습니다. 변이 유형(SNP·indel·CNV·SV)과 ClinVar 임상적 의의로 필터링하여 rsID·염색체 위치·유전자·CADD 점수·임상 주석을 확인할 수 있습니다."
      },
      {
        "name": "search_pathogenic_variants",
        "desc": "CADD 점수 임계값·표적 유전자 목록·질환 용어로 필터링하여 MyVariant.info에서 병원성 또는 병원성 추정(likely-pathogenic) 변이(ClinVar)를 검색할 수 있습니다. rsID·유전자·ClinVar 질환·CADD 점수를 확인할 수 있습니다."
      },
      {
        "name": "get_gene_orthologs",
        "desc": "HomoloGene을 사용하여 지정된 모델 생물의 직교(ortholog) 유전자를 찾을 수 있습니다. 유전자 ID(Entrez 숫자·Ensembl ENSG·유전자 심볼)로 직교유전자별 유전자 심볼·이름·taxon ID·Entrez 유전자 ID를 확인할 수 있습니다. 지원 종: \"mouse\", \"rat\", \"zebrafish\", \"fruitfly\", \"nematode\", \"pig\"."
      },
      {
        "name": "search_drug_target_genes",
        "desc": "MyGene.info의 PharmGKB 주석으로 특정 약물의 표적으로 주석된 유전자를 찾을 수 있습니다. 약물 이름(예: \"warfarin\", \"metformin\")으로 유전자 심볼·이름·PharmGKB 약물-유전자 관계 주석을 확인할 수 있습니다."
      },
      {
        "name": "get_genomic_interval_genes",
        "desc": "특정 염색체 구간과 겹치는 모든 유전자를 찾을 수 있습니다. 염색체·시작·종료(hg38 좌표)와 유전자 유형(protein-coding·lncRNA·miRNA·pseudogene) 필터로 유전자 심볼·이름·biotype·게놈 위치·요약을 확인할 수 있습니다."
      },
      {
        "name": "search_variants_by_population_frequency",
        "desc": "gnomAD 또는 ExAC 집단 데이터베이스의 대립유전자 빈도로 필터링하여 MyVariant.info에서 변이를 검색할 수 있습니다. 빈도 임계값과 비교 연산자(예: 희귀 변이는 \"< 0.01\")·집단 하위그룹(예: \"afr\", \"eas\", \"nfe\")·기능적 영향(SIFT/PolyPhen2)으로 변이를 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "chembl-server",
    "nameKo": "ChEMBL",
    "desc": "EMBL-EBI의 약물 유사(drug-like) 분자 생물활성(bioactivity) 데이터베이스인 ChEMBL을 살펴볼 수 있습니다. 화합물을 이름·SMILES·InChI로 검색하고, 생물활성 데이터(IC50·Ki·EC50)·표적 정보(유전자명·UniProt ID·종)·분석법(assay) 상세·임상 후보물질 데이터를 조회할 수 있습니다. ChEMBL ID는 CHEMBL 뒤에 숫자가 붙는 형식(예: CHEMBL25)을 사용합니다.",
    "tools": [
      {
        "name": "search_compounds",
        "desc": "이름·동의어·식별자로 ChEMBL 화합물을 검색할 수 있습니다. 물리화학적 특성(MW·logP·HBD/HBA·PSA·Ro5 위반 수)·SMILES/InChI 구조·ATC 코드·동의어·승인 상태를 확인할 수 있습니다."
      },
      {
        "name": "get_compound_info",
        "desc": "단일 ChEMBL ID(예: CHEMBL25)에 대한 전체 화합물 데이터를 조회할 수 있습니다. 물리화학적 특성(MW·logP·HBD/HBA·PSA·Ro5 위반 수)·SMILES/InChI 구조·ATC 코드·동의어·승인 상태를 확인할 수 있습니다."
      },
      {
        "name": "search_by_inchi",
        "desc": "InChI key 또는 InChI 문자열로 ChEMBL 화합물을 찾을 수 있습니다. 해당 화합물의 물리화학적 특성·SMILES/InChI 구조·ATC 코드·동의어·승인 상태 등 분자 정보를 확인할 수 있으며, 구조 식별자만 있을 때 활용합니다."
      },
      {
        "name": "get_compound_structure",
        "desc": "화합물의 화학 구조 데이터를 조회할 수 있습니다. chembl_id와 함께 canonical_smiles·standard_inchi·standard_inchi_key를 확인할 수 있어, 구조 정보만 필요할 때 활용합니다."
      },
      {
        "name": "search_similar_compounds",
        "desc": "질의 SMILES에 대한 Tanimoto 유사도를 기준으로 구조적으로 유사한 화합물을 찾을 수 있습니다. 각 결과의 전체 분자 데이터(물리화학적 특성·구조·동의어·승인 상태 등)를 함께 확인할 수 있습니다."
      },
      {
        "name": "search_targets",
        "desc": "이름이나 유형으로 생물학적 표적을 검색할 수 있습니다. target_chembl_id·pref_name·organism·target_type·UniProt accession·유전자 동의어를 확인할 수 있습니다."
      },
      {
        "name": "get_target_info",
        "desc": "단일 ChEMBL 표적 ID에 대한 전체 표적 데이터를 조회할 수 있습니다. pref_name·organism·target_type·UniProt accession·유전자 동의어와 모든 상호 참조(PDB·PDBe·GO terms·Reactome·HGNC·AlphaFoldDB 등)를 확인할 수 있습니다."
      },
      {
        "name": "get_target_compounds",
        "desc": "특정 표적 ChEMBL ID에 대해 시험된 화합물을 조회할 수 있습니다. 중복이 제거된 화합물 ChEMBL ID 목록과 활성 레코드 샘플을 확인할 수 있으며, 활성 유형(예: IC50·Ki)으로 필터링할 수 있습니다."
      },
      {
        "name": "search_by_uniprot",
        "desc": "UniProt accession으로 ChEMBL 표적을 찾을 수 있습니다. target_chembl_id·pref_name·organism·target_type·UniProt accession·유전자 동의어를 확인할 수 있습니다."
      },
      {
        "name": "get_target_pathways",
        "desc": "ChEMBL 표적에 대한 Reactome·KEGG·WikiPathways 참조를 조회할 수 있습니다. 해당 표적이 참여하는 생물학적 경로(예: \"EGFR는 어떤 경로에 속하는가?\")를 파악할 때 활용합니다."
      },
      {
        "name": "search_activities",
        "desc": "유연한 필터로 생물활성 레코드를 검색할 수 있습니다. target_chembl_id·molecule_chembl_id·assay_chembl_id·activity_type(예: IC50·Ki·Kd)의 임의 조합으로, 값·단위·분석법 컨텍스트를 포함한 활성 레코드를 확인할 수 있습니다."
      },
      {
        "name": "get_assay_info",
        "desc": "특정 ChEMBL 분석법(assay) ID에 대한 전체 분석법 상세 정보를 조회할 수 있습니다. 분석법 설명·유형·표적·종·실험 조건을 확인하여 활성 측정값의 컨텍스트를 이해할 때 활용합니다."
      },
      {
        "name": "search_by_activity_type",
        "desc": "활성 유형으로 생물활성 레코드를 찾을 수 있으며, 값 범위와 단위 필터를 적용할 수 있습니다. 예를 들어 100 nM 미만의 모든 IC50 값을 확인할 수 있습니다."
      },
      {
        "name": "get_dose_response",
        "desc": "화합물에 대한 모든 생물활성 측정값을 조회할 수 있으며, 표적으로 필터링할 수 있습니다. assay_chembl_id·target_chembl_id·activity_type·value·units·relation를 확인할 수 있어 여러 분석법에 걸쳐 화합물을 프로파일링할 때 활용합니다."
      },
      {
        "name": "compare_activities",
        "desc": "2~10개 화합물의 생물활성 데이터를 나란히 비교할 수 있습니다. 표적과 활성 유형으로 필터링할 수 있으며, 화합물별로 그룹화된 활성 레코드를 확인할 수 있습니다."
      },
      {
        "name": "search_drugs",
        "desc": "이름으로 의약품 및 임상 후보물질(임상 1상 이상, max_phase >= 1)을 검색할 수 있습니다. 해당 분자의 전체 데이터(물리화학적 특성·구조·동의어·승인 상태 등)를 확인할 수 있습니다."
      },
      {
        "name": "get_drug_info",
        "desc": "ChEMBL 화합물의 의약품 개발 상태 및 적응증 데이터를 조회할 수 있습니다. molecule_info(특성·동의어·SMILES/InChI)·development_phase(max_phase)·indications(efo_term·mesh_heading·max_phase_for_ind·ref_type)를 확인할 수 있습니다."
      },
      {
        "name": "search_drug_indications",
        "desc": "질환명 또는 적응증명으로 ChEMBL 의약품 적응증 레코드를 검색할 수 있습니다. EFO term·MeSH heading·최대 임상 단계·ClinicalTrials 참조를 포함한 적응증-의약품 매핑을 확인할 수 있습니다."
      },
      {
        "name": "get_mechanism_of_action",
        "desc": "화합물의 작용기전(mechanism) 레코드를 조회할 수 있습니다. 분자 표적·작용 유형(예: INHIBITOR·AGONIST)·결합 부위를 확인할 수 있으며, 여러 표적에 작용하는 화합물의 여러 작용기전도 살펴볼 수 있습니다."
      },
      {
        "name": "analyze_admet_properties",
        "desc": "ChEMBL 물리화학적 특성(MW·logP·HBD/HBA·PSA)으로부터 규칙 기반 ADMET 평가를 확인할 수 있습니다. 흡수·분포·약물 유사성(drug-likeness)에 대한 정성적 평가를 살펴볼 수 있으며, Lipinski/PSA 임계값으로부터 추론된 결과입니다."
      },
      {
        "name": "calculate_descriptors",
        "desc": "화합물의 ChEMBL 물리화학적 기술자(descriptor)를 구조화된 그룹으로 확인할 수 있습니다. 분자량·logP·HBD/HBA·PSA·회전 가능 결합 수·방향족 고리 수·중원자 수·Ro5 위반 수·SMILES/InChI를 살펴볼 수 있습니다."
      },
      {
        "name": "predict_solubility",
        "desc": "ChEMBL의 logP와 PSA를 사용한 규칙 기반 수용해도(aqueous solubility)와 막 투과성(membrane permeability) 분류를 확인할 수 있습니다. 용해도와 투과성 각각에 대한 predicted_class(High/Moderate/Low)를 살펴볼 수 있으며, 휴리스틱 추정치입니다."
      },
      {
        "name": "assess_drug_likeness",
        "desc": "Lipinski의 Rule of Five(MW≤500·logP≤5·HBD≤5·HBA≤10)와 Veber 규칙(회전 가능 결합 수≤10·PSA≤140)을 적용한 약물 유사성(drug-likeness) 평가를 확인할 수 있습니다. 위반 목록과 각 규칙 세트에 대한 통과/실패를 살펴볼 수 있습니다."
      },
      {
        "name": "substructure_search",
        "desc": "주어진 부분구조(SMILES로 정의, 예: 벤젠 고리의 경우 \"c1ccccc1\")를 포함하는 ChEMBL 화합물을 찾을 수 있습니다. 해당 질의 단편을 구조에 포함하는 모든 화합물의 전체 분자 데이터를 확인할 수 있습니다."
      },
      {
        "name": "get_external_references",
        "desc": "화합물 또는 표적에 대한 외부 데이터베이스 상호 참조를 조회할 수 있습니다. 분자 ChEMBL ID와 표적 ChEMBL ID 모두에 대해, 데이터베이스별(PubChem·DrugBank·Wikipedia·KEGG·Reactome 등)로 그룹화된 참조를 직접 연결 URL과 함께 확인할 수 있습니다."
      },
      {
        "name": "advanced_search",
        "desc": "물리화학적 특성 범위로 화합물을 필터링하여 찾을 수 있습니다. 분자량(min_mw·max_mw)·친유성(min_logp·max_logp)·수소 결합 공여자(max_hbd)·수소 결합 수용자(max_hba)를 조합할 수 있으며, 일치하는 화합물의 전체 분자 데이터를 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "clinical-trials-server",
    "nameKo": "Clinical Trials",
    "desc": "전 세계 임상시험을 등록하는 미국 NIH 레지스트리인 ClinicalTrials.gov를 살펴볼 수 있습니다. 질환·중재(intervention)·스폰서·위치·날짜 범위로 임상시험을 검색하고, 전체 연구 프로토콜·적격성 기준(eligibility criteria)·결과 지표(outcome)·시험 결과를 조회할 수 있습니다. 시험 ID는 NCT 뒤에 8자리 숫자가 붙는 형식(예: NCT02576665)을 사용합니다.",
    "tools": [
      {
        "name": "search_studies",
        "desc": "다양한 필터로 임상시험을 검색할 수 있습니다. interventions(약물명·유형)·conditions·phase·sponsor·status를 확인할 수 있으며, 진행 중인 시험은 status=RECRUITING·ACTIVE_NOT_RECRUITING로 살펴볼 수 있습니다."
      },
      {
        "name": "get_study_details",
        "desc": "NCT ID로 특정 임상시험의 상세 정보를 조회할 수 있습니다. 식별 정보·상태·설계(단계·등록 인원)·스폰서·질환·연구자(PI 이름/소속)·중재(약물명/유형)·1차 및 2차 결과 지표·적격성·위치·결과(완료된 경우)를 확인할 수 있습니다."
      },
      {
        "name": "search_by_location",
        "desc": "지리적 위치로 임상시험을 찾을 수 있습니다. country 필터로 특정 국가에서 진행되는 시험을 확인할 수 있습니다."
      },
      {
        "name": "search_by_condition",
        "desc": "특정 의학적 질환에 초점을 둔 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "get_trial_statistics",
        "desc": "특정 필드별로 그룹화된 임상시험 집계 통계를 조회할 수 있습니다. status·phase·studyType·condition·sponsor·intervention 기준으로 시험 건수를 확인할 수 있으며, groupBy=intervention으로 약물/중재명별 시험 건수를 살펴볼 수 있습니다."
      },
      {
        "name": "search_by_sponsor",
        "desc": "스폰서 또는 기관으로 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "search_by_intervention",
        "desc": "중재 또는 치료 유형으로 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "search_by_date_range",
        "desc": "시작일 또는 완료일 범위로 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "get_studies_with_results",
        "desc": "결과가 게시된 완료된 임상시험을 찾을 수 있습니다."
      },
      {
        "name": "search_rare_diseases",
        "desc": "희귀질환 및 희귀의약품 적응 질환(orphan condition)에 대한 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "get_pediatric_studies",
        "desc": "소아 및 청소년을 위해 특별히 설계된 임상시험을 찾을 수 있습니다."
      },
      {
        "name": "get_similar_studies",
        "desc": "NCT ID로 특정 연구와 유사한 임상시험을 찾을 수 있습니다."
      },
      {
        "name": "search_by_primary_outcome",
        "desc": "1차 결과 지표(primary outcome) 또는 평가변수(endpoint)로 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "search_by_eligibility_criteria",
        "desc": "상세한 적격성 기준(eligibility criteria)을 기반으로 임상시험을 검색할 수 있습니다."
      },
      {
        "name": "get_study_timeline",
        "desc": "연구의 상세 타임라인 및 마일스톤 정보를 조회할 수 있습니다."
      }
    ]
  },
  {
    "sys": "ensembl-server",
    "nameKo": "Ensembl",
    "desc": "Ensembl 유전체 데이터: 유전자·전사체·서열·변이·조절 요소·상동유전자·어셈블리 메타데이터를 확인할 수 있습니다.",
    "tools": [
      {
        "name": "lookup_gene",
        "desc": "안정 ID(stable ID)로 Ensembl 유전자의 심볼, biotype(protein_coding·lncRNA 등), 염색체, 유전체 좌표, 가닥(strand), 표시명, 설명을 확인할 수 있습니다. 특정 유전자의 기본 정보를 파악할 때 활용합니다."
      },
      {
        "name": "get_transcripts",
        "desc": "Ensembl 유전자에 속한 모든 전사체의 전사체 ID, biotype, 시작·종료 위치, 엑손 구조를 확인할 수 있습니다. biotype(예: protein_coding)으로 특정 유형의 전사체만 살펴볼 수 있습니다."
      },
      {
        "name": "search_genes",
        "desc": "유전자 심볼이나 외부 식별자로 일치하는 Ensembl 엔터티를 검색하여 Ensembl ID, 유형(gene·transcript·translation), 버전을 확인할 수 있습니다. BRCA1·TP53 같은 심볼로부터 Ensembl ID를 찾을 때 활용합니다."
      },
      {
        "name": "get_sequence",
        "desc": "Ensembl ID(유전자·전사체·엑손)에 대한 유전체 DNA 서열을 조회할 수 있습니다. 안정 ID와 함께 해당 구간의 서열 문자열을 확인할 때 활용합니다."
      },
      {
        "name": "get_cds_sequence",
        "desc": "Ensembl 전사체의 CDS(코딩 서열)를 개시 코돈부터 종결 코돈까지 확인할 수 있습니다. 전체 유전체 구간이 아닌 실제 코딩 영역의 뉴클레오타이드 서열을 얻을 때 활용합니다."
      },
      {
        "name": "translate_sequence",
        "desc": "입력 서열을 그대로 되돌려주는 자리표시자(stub) 도구로, 실제 번역은 수행하지 않습니다. 실제 단백질 번역에는 get_cds_sequence의 CDS를 전용 번역 도구와 함께 사용합니다."
      },
      {
        "name": "get_homologs",
        "desc": "Ensembl Compara에서 상동유전자(homolog) 쌍의 Ensembl ID, 유전자 심볼, 종, 상동성 하위유형(ortholog_one2one·ortholog_one2many·within_species_paralog 등)을 확인할 수 있습니다. type으로 ortholog/paralog를, target_species로 특정 종을 한정하여 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_tree",
        "desc": "CAFE 유전자 트리로부터 유전자 패밀리의 진화 요약을 확인할 수 있습니다. 트리 ID, p-value, 전체 종 수, 분기군(clade)별 확장(expansion)·수축(contraction) 사건을 살펴볼 수 있으며, ENSGT로 시작하는 트리 ID(예: ENSGT00390000003602)로 조회합니다."
      },
      {
        "name": "get_variants",
        "desc": "유전체 영역 내 모든 변이의 요약을 확인할 수 있습니다. 결과(consequence) 유형별로 그룹화된 변이 수와, 임상적 의의(clinical significance) 주석(ClinVar·OMIM 등)이 포함된 변이 목록을 살펴볼 때 활용합니다."
      },
      {
        "name": "get_variant_ids",
        "desc": "유전체 영역 내에서 특정 결과(consequence) 유형으로 필터링된 변이 ID(rs 번호) 목록을 확인할 수 있습니다. 관심 변이의 rs ID를 추려 상세 VEP 주석으로 이어갈 때 활용합니다."
      },
      {
        "name": "get_variant_consequences",
        "desc": "하나 이상의 변이 ID(rs 번호)에 대해 Ensembl VEP(Variant Effect Predictor) 주석을 확인할 수 있습니다. 예측된 결과(consequence) 용어, 영향을 받는 전사체, 아미노산 변화, SIFT/PolyPhen 점수, 동일 위치 변이(colocated variants)를 살펴볼 수 있으며 HGVS 표기·UniProt 상호 참조·정규(canonical) 전사체 표시도 함께 확인할 수 있습니다."
      },
      {
        "name": "get_regulatory_features",
        "desc": "유전체 영역과 겹치는 Ensembl 조절 요소(프로모터·인핸서·CTCF 결합 부위·열린 염색질·TF 결합 부위 등)를 확인할 수 있습니다. 해당 영역의 유전자 발현 조절 요소를 파악할 때 활용합니다."
      },
      {
        "name": "get_motif_features",
        "desc": "Ensembl Regulatory Build에서 유전체 영역 내 전사인자 결합 모티프(motif) 요소의 결합 행렬명, 점수, 좌표, 가닥(strand)을 확인할 수 있습니다."
      },
      {
        "name": "get_xrefs",
        "desc": "Ensembl 유전자·전사체·단백질 ID에 대한 상호 참조(외부 데이터베이스 링크)의 데이터베이스명, 기본 accession, 표시 라벨, 설명을 확인할 수 있습니다. external_db(예: UniProtKB/Swiss-Prot·HGNC·RefSeq_mRNA)로 특정 데이터베이스만 살펴볼 수 있습니다."
      },
      {
        "name": "map_coordinates",
        "desc": "두 유전체 어셈블리(assembly) 간 좌표 변환 결과를 확인할 수 있습니다. GRCh37(hg19)과 GRCh38(hg38) 사이로 위치를 변환(lift over)할 때 활용합니다."
      },
      {
        "name": "list_species",
        "desc": "Ensembl REST API가 지원하는 종 목록을 확인할 수 있습니다. 표시명이나 일반명으로 검색하여(예: human → homo_sapiens, mouse → mus_musculus) 다른 도구에서 사용하는 species name 필드를 파악할 때 활용합니다."
      },
      {
        "name": "get_assembly_info",
        "desc": "특정 종의 유전체 어셈블리 메타데이터를 확인할 수 있습니다. 어셈블리명, INSDC accession, 염색체 이름 목록, 염색체별 길이를 살펴보고 영역 문자열에 쓸 염색체 식별자를 파악할 때 활용합니다."
      },
      {
        "name": "get_karyotype",
        "desc": "특정 종의 염색체 핵형(karyotype)과 세포유전학적 밴드(cytogenetic band)의 이름·시작·종료 위치를 확인할 수 있습니다. 유전체 좌표를 세포유전학적 밴드(예: 17q12)에 매핑할 때 활용합니다."
      },
      {
        "name": "batch_gene_lookup",
        "desc": "여러 Ensembl 유전자 ID에 대해 lookup_gene과 동일한 필드(심볼·biotype·좌표 등)를 한 번에 확인할 수 있습니다. 다수의 Ensembl ID를 유전자 심볼과 메타데이터로 변환할 때 활용합니다."
      },
      {
        "name": "batch_sequence_fetch",
        "desc": "여러 Ensembl ID에 대한 유전체 서열을 한 번에 확인할 수 있습니다. 각 ID의 안정 ID와 서열 문자열을 함께 살펴볼 때 활용합니다."
      }
    ]
  },
  {
    "sys": "go-server",
    "nameKo": "Gene Ontology",
    "desc": "유전자/단백질의 생물학적 기능에 대한 표준화된 용어 체계인 Gene Ontology(GO)를 살펴볼 수 있습니다. GO term과 유전자 간의 양방향 매핑을 통해 생물학적 개념으로부터 GO ID를 식별하거나(예: \"kinase activity를 나타내는 GO term은 무엇인가?\"), 유전자의 GO 주석(어떤 기능/과정/위치를 갖는가)을 조회하거나, 특정 GO term에 주석된 모든 유전자를 확인할 수 있습니다. GO term은 GO:XXXXXXX 형식을, 유전자 입력에는 UniProt accession(예: P00533)을 사용하며 molecular_function·biological_process·cellular_component 세 가지 네임스페이스를 다룹니다.",
    "tools": [
      {
        "name": "search_go_terms",
        "desc": "키워드·이름 단편·정의 텍스트로 Gene Ontology term을 검색할 수 있습니다. GO ID·term 이름·정의·네임스페이스를 확인할 수 있으며, 세 가지 GO 네임스페이스 중 하나로 한정할 수 있습니다."
      },
      {
        "name": "get_go_term",
        "desc": "안정 ID로 단일 GO term의 전체 정보를 조회할 수 있습니다. term 이름·전체 정의·네임스페이스(molecular_function / biological_process / cellular_component)·폐기(obsolescence) 상태·동의어·폐기된 term의 대체 ID를 확인할 수 있습니다."
      },
      {
        "name": "validate_go_id",
        "desc": "GO 식별자가 올바른 형식(GO:XXXXXXX, 7자리 숫자)인지, 현재 Gene Ontology에 존재하는지 확인할 수 있습니다. 형식 유효성·존재 여부와 기본 term 메타데이터(이름·네임스페이스·폐기 상태)를 확인할 수 있습니다."
      },
      {
        "name": "get_ontology_stats",
        "desc": "세 가지 GO 네임스페이스에 대한 참조 정보를 확인할 수 있습니다. molecular_function(루트 GO:0003674)·biological_process(루트 GO:0008150)·cellular_component(루트 GO:0005575)와 함께, 모든 GO 증거 코드(evidence code) 범주(실험적: EXP/IDA/IPI/IMP/IGI/IEP·고처리량·계산적: ISS/ISO/ISA/IBA 등·저자/큐레이터 진술·전자적: IEA)를 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_go_annotations",
        "desc": "특정 유전자 또는 단백질에 주석된 모든 GO term을 조회할 수 있습니다. 입력은 UniProt accession(예: EGFR은 P00533·BRCA1은 P38398)을 사용하며, 이름·네임스페이스·증거 코드가 포함된 GO term을 확인할 수 있습니다. taxon_id=9606으로 사람 주석만 살펴볼 수 있습니다."
      },
      {
        "name": "get_go_term_genes",
        "desc": "특정 GO term에 주석된 모든 유전자/단백질을 조회할 수 있습니다. UniProt accession·유전자 심볼·taxon ID·증거 코드가 포함된 유전자 산물(gene product) 목록을 확인할 수 있으며, taxon_id=9606으로 사람에 한정하거나 evidence로 특정 증거 코드(예: 실험적인 경우 \"EXP\"·전자적인 경우 \"IEA\")로 살펴볼 수 있습니다."
      }
    ]
  },
  {
    "sys": "kegg-server",
    "nameKo": "KEGG",
    "desc": "KEGG 생물학 지식 데이터: 경로(pathway)·유전자·화합물·반응·효소·질환·약물·모듈·KO 오솔로지·글리칸·BRITE 분류를 확인할 수 있습니다.",
    "tools": [
      {
        "name": "list_organisms",
        "desc": "KEGG에 등록된 모든 생물종을 3~4글자 생물종 코드와 함께 확인할 수 있습니다(예: 사람 hsa, 마우스 mmu, 대장균 eco). 다른 도구의 입력으로 쓸 생물종 코드를 파악할 때 활용합니다."
      },
      {
        "name": "search_pathways",
        "desc": "키워드나 경로 이름으로 KEGG 경로를 검색하여 경로 ID(예: hsa00010)를 확인할 수 있습니다. 생물종 코드로 특정 종에 한정하여 살펴볼 수 있습니다."
      },
      {
        "name": "get_pathway_info",
        "desc": "KEGG 경로의 구성 유전자, 대사물질, 반응, 질환·약물 링크, 외부 DB 상호참조 등 전체 상세 정보를 확인할 수 있습니다. 경로 ID(예: hsa00010·map00010)로 조회합니다."
      },
      {
        "name": "get_pathway_genes",
        "desc": "특정 KEGG 경로에 참여하는 모든 유전자 ID를 확인할 수 있습니다. 생물종별 경로 ID(예: 사람 hsa00010, 마우스 mmu00010)로 조회합니다."
      },
      {
        "name": "search_genes",
        "desc": "이름·심볼·키워드로 KEGG 유전자를 검색하여 org:id 형식의 유전자 ID(예: 사람 EGFR은 hsa:1956)를 확인할 수 있습니다. organism_code(예: hsa·mmu)로 단일 종에 한정하여 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_info",
        "desc": "유전자의 기능·정의, 연계된 경로, KO 오솔로지 할당, 유전체 위치, 모티프, 외부 DB 상호참조를 확인할 수 있으며 아미노산·염기서열도 함께 살펴볼 수 있습니다. org:id 형식(예: hsa:1956)으로 조회합니다."
      },
      {
        "name": "search_compounds",
        "desc": "이름·분자식·질량으로 KEGG COMPOUND 데이터베이스를 검색하여 화합물 ID(예: ATP는 C00002, 포도당은 C00031)를 확인할 수 있습니다. 분자식(예: C6H12O6), 정확 질량, 분자량 기준으로도 살펴볼 수 있습니다."
      },
      {
        "name": "get_compound_info",
        "desc": "화합물의 분자식, 분자량, 연계된 반응·경로·효소, 외부 DB 상호참조(예: ChEBI·PubChem)를 확인할 수 있습니다."
      },
      {
        "name": "get_reaction_info",
        "desc": "생화학 반응의 반응식, 기질·생성물 화합물, 촉매 효소(EC 번호), 연계된 경로를 확인할 수 있습니다. 반응 ID(예: R00001)로 조회합니다."
      },
      {
        "name": "search_enzymes",
        "desc": "EC 번호나 효소 이름으로 KEGG ENZYME 데이터베이스를 검색하여 효소 항목 ID(예: ec:1.1.1.1)를 확인할 수 있습니다."
      },
      {
        "name": "get_enzyme_info",
        "desc": "효소가 촉매하는 반응, 기질·생성물, 연계된 유전자, 관련 경로를 확인할 수 있습니다. EC 번호는 ec:1.1.1.1 형식으로 조회합니다."
      },
      {
        "name": "search_diseases",
        "desc": "이름이나 키워드로 KEGG DISEASE 데이터베이스의 인간 질환 항목을 검색하여 질환 ID(예: 대장암은 H00001)를 확인할 수 있습니다."
      },
      {
        "name": "get_disease_info",
        "desc": "KEGG DISEASE에서 질환의 원인 유전자, 관련 경로, 승인된 약물을 확인할 수 있습니다."
      },
      {
        "name": "search_drugs",
        "desc": "약물명·분자 표적·치료 적응증으로 KEGG DRUG 데이터베이스를 검색하여 약물 ID(예: 메트포르민은 D00001)를 확인할 수 있습니다."
      },
      {
        "name": "get_drug_info",
        "desc": "약물의 분자 표적, 작용 기전, 승인된 치료 적응증, 외부 DB 상호참조를 확인할 수 있습니다."
      },
      {
        "name": "get_drug_interactions",
        "desc": "여러 KEGG 약물 ID 사이의 알려진 약물-약물 상호작용(DDI)을 확인할 수 있습니다. 다제 병용(polypharmacy) 안전성을 파악할 때 활용합니다."
      },
      {
        "name": "search_modules",
        "desc": "이름이나 대사 기능으로 KEGG MODULE 데이터베이스의 기능 모듈(M-번호)을 검색하여 모듈 ID(예: M00001)를 확인할 수 있습니다. 모듈은 경로 내 개별 기능 단위를 나타내는 정제된 반응 집합입니다."
      },
      {
        "name": "get_module_info",
        "desc": "모듈의 구성 반응, 화합물, KO 정의, 관련 경로를 확인할 수 있습니다. 모듈 ID(예: M00001)로 조회합니다."
      },
      {
        "name": "search_ko_entries",
        "desc": "유전자 이름이나 분자 기능으로 KEGG ORTHOLOGY(KO) 항목을 검색하여 KO ID(예: K00001)를 확인할 수 있습니다. KO ID는 생물종과 무관하게 기능적 오솔로그를 정의하여 종 간 기능 비교에 활용합니다."
      },
      {
        "name": "get_ko_info",
        "desc": "KO 항목의 EC 번호, 연계된 경로·모듈, 모든 생물종에 걸친 구성 유전자를 확인할 수 있습니다. KO ID(예: K00001)로 조회합니다."
      },
      {
        "name": "search_glycans",
        "desc": "글리칸 이름이나 당 조성으로 KEGG GLYCAN 데이터베이스를 검색하여 글리칸 ID(예: G00001)를 확인할 수 있습니다."
      },
      {
        "name": "get_glycan_info",
        "desc": "글리칸의 당 조성, 연계된 반응·효소, 관련 경로를 확인할 수 있습니다."
      },
      {
        "name": "search_brite",
        "desc": "KEGG BRITE 계층 분류 데이터를 검색하여 BRITE 항목 ID를 확인할 수 있습니다. BRITE는 유전자·화합물·약물 등을 ATC 약물 분류, 효소 분류, 경로 맵과 같은 기능 범주로 조직화합니다."
      },
      {
        "name": "get_brite_info",
        "desc": "주어진 항목의 BRITE 계층 내용을 확인할 수 있습니다. 약물 분류, 효소 계열, 경로 맵 구조와 같은 기능적 그룹화를 살펴볼 때 활용합니다."
      },
      {
        "name": "get_pathway_compounds",
        "desc": "특정 KEGG 경로에 참여하는 모든 대사물질·화합물 ID를 확인할 수 있습니다."
      },
      {
        "name": "get_pathway_reactions",
        "desc": "KEGG 경로와 연관된 모든 반응 ID(예: R00001)를 확인할 수 있습니다. 경로 내 개별 대사 단계를 추적할 때 활용하며, 참조 경로 ID(예: map00010·rn00010)로 조회합니다."
      },
      {
        "name": "get_compound_reactions",
        "desc": "화합물이 기질 또는 생성물로 참여하는 모든 생화학 반응(반응 ID, 예: rn:R00001)을 확인할 수 있습니다."
      },
      {
        "name": "get_gene_orthologs",
        "desc": "유전자를 해당 KO(KEGG Orthology) 식별자로 매핑한 결과를 확인할 수 있습니다. 이 KO ID로 모든 생물종에서 오솔로그 유전자를 살펴볼 수 있으며, org:id 형식(예: hsa:672)으로 조회합니다."
      },
      {
        "name": "batch_entry_lookup",
        "desc": "여러 KEGG 항목(유전자·화합물·반응·경로 등 모든 항목 유형)의 정보를 한 번에 확인할 수 있습니다."
      },
      {
        "name": "convert_identifiers",
        "desc": "KEGG와 외부 데이터베이스(예: NCBI Gene ID·UniProt·ChEBI) 사이의 식별자 변환 결과를 확인할 수 있습니다. 외부 ID를 KEGG 체계로 가져오거나 KEGG ID를 다른 시스템으로 내보낼 때 활용합니다."
      },
      {
        "name": "find_related_entries",
        "desc": "KEGG 항목 간 데이터베이스 상호 링크(예: 경로→화합물, 화합물→반응, 유전자→경로)를 확인할 수 있습니다. KEGG 데이터베이스 전반의 생물학적 네트워크 연결을 탐색할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "ncbi-datasets-server",
    "nameKo": "NCBI Datasets",
    "desc": "NCBI의 유전체 어셈블리, 유전자 레코드, 분류(taxonomy) 데이터를 살펴볼 수 있습니다. 유전체 어셈블리(RefSeq/GenBank)를 검색·다운로드하고, 심볼이나 ID로 유전자 정보를 조회하며, 어셈블리 품질 지표 및 주석(annotation) 보고서와 분류 계통(lineage)을 확인할 수 있습니다. 어셈블리 ID는 GCA/GCF 형식을 사용합니다(예: GCF_000001405).",
    "tools": [
      {
        "name": "search_genomes",
        "desc": "생물종 이름이나 NCBI 분류 ID(taxonomy ID)로 NCBI 유전체 어셈블리를 찾을 수 있습니다. 어셈블리 등록번호(GCF/GCA)·어셈블리 수준(Complete/Chromosome/Scaffold/Contig)·공개일·품질 통계를 확인할 수 있습니다."
      },
      {
        "name": "get_genome_info",
        "desc": "특정 등록번호(GCF/GCA)에 대한 전체 유전체 어셈블리 정보를 조회할 수 있습니다. 생물종 이름·어셈블리 수준·공개일·제출자·품질 통계(N50, GC%, 염색체 수, contig 수)와 가능한 경우 주석 요약까지 확인할 수 있습니다."
      },
      {
        "name": "get_genome_annotation",
        "desc": "유전체 어셈블리의 주석 통계를 확인할 수 있습니다. 유전자 수(단백질 코딩·위유전자·ncRNA 등)·BUSCO 완전성 점수·주석 제공자·공개일을 구조화된 요약 형태로 살펴볼 수 있습니다."
      },
      {
        "name": "get_assembly_quality",
        "desc": "유전체의 상세 어셈블리 품질 지표를 확인할 수 있습니다. 총 서열 길이·염색체/스캐폴드/contig 수·contig 및 스캐폴드의 N50/L50 값·GC%·BUSCO 완전성 점수(단일 카피/중복/단편화/누락)를 살펴보며 서로 다른 버전 간 어셈블리 품질을 비교할 때 활용합니다."
      },
      {
        "name": "get_assembly_reports",
        "desc": "유전체 어셈블리의 서열 보고서를 조회하여 개별 서열(염색체·스캐폴드·미배치 contig)을 등록번호·길이·어셈블리 역할과 함께 확인할 수 있습니다."
      },
      {
        "name": "compare_genomes",
        "desc": "여러 유전체 어셈블리를 나란히 비교할 수 있습니다. 각 등록번호에 대해 생물종 정보·어셈블리 수준·품질 지표(N50, GC%, 염색체 수)를 함께 살펴볼 수 있어 서로 다른 어셈블리 버전이나 근연 종의 어셈블리를 비교할 때 활용합니다."
      },
      {
        "name": "download_genome_data",
        "desc": "NCBI 유전체 데이터 패키지의 다운로드 URL을 확인할 수 있습니다. 어셈블리 FASTA·GFF3 주석·RNA 서열·CDS·단백질·서열 보고서를 받을 수 있는 미리 구성된 URL을 얻을 수 있습니다."
      },
      {
        "name": "batch_assembly_info",
        "desc": "여러 유전체 등록번호의 어셈블리 정보를 한 번에 조회할 수 있습니다. 각 등록번호에 대해 get_genome_info와 동일한 항목을 확인할 수 있습니다."
      },
      {
        "name": "search_genes",
        "desc": "유전자 심볼 및/또는 생물종으로 NCBI 유전자 레코드를 찾을 수 있습니다. 유전자 ID·심볼·설명·염색체 위치·전사체/단백질 수를 확인할 수 있습니다."
      },
      {
        "name": "get_gene_info",
        "desc": "유전자 ID로 NCBI의 상세 유전자 정보를 조회할 수 있습니다. 유전자 심볼·설명·생물종·염색체·유전체 좌표·전사체 수·단백질 수·Swiss-Prot 등록번호를 확인할 수 있습니다."
      },
      {
        "name": "search_taxonomy",
        "desc": "이름으로 NCBI 분류에서 생물종을 찾을 수 있습니다. 학명·분류 ID(tax ID)·분류 계급을 확인하여 다른 도구에 사용할 tax ID를 파악할 때 활용합니다."
      },
      {
        "name": "get_taxonomy_info",
        "desc": "NCBI 분류 ID로 생물종의 전체 분류 체계와 메타데이터를 조회할 수 있습니다. 학명·일반명·계급·계통(역/계/문/강/목/과/속)·유전체/유전자 수 통계를 확인할 수 있습니다."
      },
      {
        "name": "get_phylogenetic_tree",
        "desc": "NCBI 분류 ID로 지정한 분류군 집합을 연결하는 분류 하위 트리를 확인할 수 있습니다. 주어진 분류군 간 관계를 보여주는 최소 신장 트리(minimal spanning tree)로 여러 생물종 간 진화적 관계를 살펴볼 때 활용합니다."
      },
      {
        "name": "search_proteins",
        "desc": "유전자 심볼과 생물종, 또는 단백질 등록번호로 NCBI 단백질 산물 레코드를 찾을 수 있습니다. 전사체 등록번호·단백질 등록번호·동형체(isoform) 이름·길이를 포함한 유전자 수준 산물 정보를 확인할 수 있습니다."
      },
      {
        "name": "get_protein_info",
        "desc": "특정 RefSeq 단백질 등록번호의 단백질 산물 정보를 조회할 수 있습니다. 소속 유전자·생물종·관련 전사체(등록번호·이름·길이)·동형체 정보를 확인할 수 있습니다."
      },
      {
        "name": "search_virus_genomes",
        "desc": "바이러스 분류군 ID 또는 이름으로 NCBI 바이러스 유전체 서열을 찾을 수 있습니다. 유전체 등록번호·완전성·단백질 수·숙주·분리주(isolate)·지리적 위치·채취일을 확인할 수 있습니다."
      },
      {
        "name": "get_virus_info",
        "desc": "NCBI 등록번호로 특정 바이러스 유전체의 상세 정보를 조회할 수 있습니다. 바이러스 분류·유전체 길이·완전성·주석 상태·단백질 수·숙주 생물·분리주 이름·지리적 위치·채취/공개일을 확인할 수 있습니다."
      },
      {
        "name": "get_database_stats",
        "desc": "NCBI 유전자 데이터베이스에서 생물종의 유전자 수 통계를 확인할 수 있습니다. 전체 유전자 수·단백질 코딩 유전자·ncRNA 유전자·위유전자 및 기타 범주의 수를 살펴볼 수 있습니다."
      },
      {
        "name": "validate_accession",
        "desc": "유전체 어셈블리 등록번호(GCF/GCA)가 NCBI에 존재하는지 검증하고 현재 상태를 확인할 수 있습니다. 유효성 여부·현재 표준(canonical) 등록번호·생물종 이름·어셈블리 수준·어셈블리 상태(latest/replaced/suppressed)를 살펴볼 수 있어 논문이나 데이터베이스의 등록번호가 여전히 최신인지 파악할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "fda-server",
    "nameKo": "OpenFDA",
    "desc": "미국 FDA의 약물 및 의료기기 공개 데이터를 살펴볼 수 있습니다. 약물 이상반응 보고(FAERS), 약물 라벨(처방 정보), NDC 약물 제품 코드, 약물 리콜, 약물 공급 부족 통지, 의료기기 510(k) 허가, 기기 분류, 기기 이상반응, 기기 리콜을 확인할 수 있습니다. 모든 데이터는 FDA의 공개 OpenFDA 엔드포인트에서 제공됩니다.",
    "tools": [
      {
        "name": "search_drug_adverse_events",
        "desc": "환자·의료진·제조사가 제출한 자발적 약물 이상반응 보고를 FDA 이상반응 보고 시스템(FAERS)에서 확인할 수 있습니다. 의심 약물·이상반응·환자 인구통계·중대성 분류를 담은 안전성 보고를 살펴볼 수 있고, 약물명(상품명/일반명/의약품명)·특정 반응(MedDRA 용어)·제조사·환자 성별·발생 국가·보고 접수일 범위로 좁혀볼 수 있습니다. 중대 보고(사망·입원·생명 위협 등)만 확인하거나, .exact 필드 경로(예: patient.reaction.reactionmeddrapt.exact)로 반응별 빈도 집계를 얻을 수도 있습니다."
      },
      {
        "name": "search_drug_labels",
        "desc": "처방 정보 및 일반의약품(OTC) 약물 정보를 담은 FDA 약물 라벨링 데이터베이스(Structured Product Labels / SPL)를 확인할 수 있습니다. 적응증 및 용법·금기·경고·용량 및 투여·이상반응·약물 상호작용·유효성분 등의 라벨 섹션을 살펴볼 수 있으며, 상품명·일반명·유효성분·제조사·투여 경로(ORAL, TOPICAL, INTRAVENOUS 등)·제품 유형(HUMAN PRESCRIPTION DRUG, HUMAN OTC DRUG 등)으로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_drug_ndc",
        "desc": "등록된 약물 제품과 포장 정보를 담은 FDA 국가 약물 코드(NDC) 디렉터리를 확인할 수 있습니다. 제품 NDC·포장 NDC·라벨러명·제형·투여 경로·마케팅 범주(NDA, ANDA, OTC monograph 등)·신청 번호·마케팅 일자·유효성분 정보를 살펴볼 수 있으며, 제품/포장 NDC·고유(상품)명·비고유(일반)명·라벨러명·제형·투여 경로·유효 물질명으로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_drug_recalls",
        "desc": "FDA 약물 리콜 시행 보고서를 확인할 수 있습니다. 리콜 번호·제품 설명·리콜 시행 회사·리콜 분류(Class I = 중대한 건강 위해 또는 사망 위험이 가장 큼, Class II = 일시적이거나 의학적으로 회복 가능한 건강 위해, Class III = 건강 위해 가능성이 낮음)·리콜 상태·리콜 사유·유통 패턴·제품 수량·리콜 개시일을 살펴볼 수 있으며, 제품 설명·리콜 시행 회사·리콜 등급·상태(Ongoing, Completed, Terminated, Pending)·주(state)·국가·사유 키워드·개시일 범위로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_drugs_fda",
        "desc": "신약 신청(NDA)·약식 신약 신청(ANDA)·생물의약품 허가 신청(BLA)을 포함한 FDA 승인 약물 신청을 Drugs@FDA 데이터베이스에서 확인할 수 있습니다. 스폰서명·신청 번호·상품명 및 일반명·제품 목록(제형·투여 경로·마케팅 상태·유효성분)·제출 이력을 살펴볼 수 있으며, 스폰서명·신청 번호·상품명·일반명·유효성분·제형·마케팅 상태(Prescription, Over-the-counter, Discontinued)로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_drug_shortages",
        "desc": "현재 및 과거에 해소된 약물 공급 부족에 대한 FDA 약물 공급 부족 통지를 확인할 수 있습니다. 일반명·회사명·제형·투여 경로·부족 상태(Current, To Be Discontinued)·예상 재공급일·최종 갱신일을 살펴볼 수 있으며, 제품명·일반명·유효성분·부족 상태·제형으로 좁혀볼 수 있습니다. FDA는 주로 의학적으로 필수적인 약물에 대한 공급 부족 보고를 수집합니다."
      },
      {
        "name": "search_device_510k",
        "desc": "허가받은 의료기기에 대한 FDA 510(k) 시판 전 신고 데이터베이스를 확인할 수 있습니다. 510(k) 경로는 선행 기기(predicate device)와 실질적으로 동등한 기기가 시장에 진입할 수 있도록 합니다. k_number·기기명·신청 회사·제품 코드·허가 유형(Traditional, Abbreviated, Special)·결정일·결정(Substantially Equivalent / Not Substantially Equivalent)·기기 등급을 살펴볼 수 있으며, 기기명·신청 회사·제품 코드·허가 유형·결정일 범위로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_device_classifications",
        "desc": "기기의 규제 등급과 적용 규정을 확인하기 위해 FDA 의료기기 제품 분류 데이터베이스를 살펴볼 수 있습니다. 기기는 Class I(최저 위험, 일반 규제)·Class II(중간 위험, 특별 규제, 흔히 510(k) 필요)·Class III(최고 위험, 시판 전 승인(PMA) 필요)로 분류됩니다. 기기명·기기 등급·의료 전문 패널·제품 코드·규정 번호·정의를 확인할 수 있으며, 기기명·기기 등급(1/2/3)·의료 전문 분야(예: cardiovascular, orthopedic, neurological)·제품 코드·규정 번호(21 CFR part)로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_device_adverse_events",
        "desc": "기기 이상반응 보고에 대한 FDA 의료기기 보고(MDR) 데이터베이스를 확인할 수 있습니다. MDR은 기기 관련 사망·중대한 부상·오작동에 대한 의무 보고와 의료진·환자의 자발적 보고를 수집하는 시판 후 감시 시스템입니다. 보고 번호·이벤트 유형(사망·부상·오작동)·기기 상품명/일반명·제조사·모델 번호·기기 등급·환자 인구통계·이벤트 서술을 살펴볼 수 있으며, 기기명·상품명·제조사·제품 코드·이벤트 유형·환자 성별·접수일 범위로 좁혀볼 수 있습니다."
      },
      {
        "name": "search_device_recalls",
        "desc": "FDA 의료기기 리콜 시행 보고서를 확인할 수 있습니다. 기기 리콜에는 제조사가 자발적으로 개시한 리콜과 FDA가 명령한 의무 리콜이 모두 포함됩니다. 리콜 번호·제품 설명·리콜 시행 회사·리콜 분류(Class I = 가장 중대, Class II = 중간, Class III = 가장 경미)·상태·리콜 사유·리콜 개시일·유통 패턴·제품 수량을 살펴볼 수 있으며, 제품 설명·리콜 시행 회사·리콜 등급·상태·제품 코드·리콜 개시일 범위로 좁혀볼 수 있습니다."
      }
    ]
  },
  {
    "sys": "opengenes-server",
    "nameKo": "OpenGenes",
    "desc": "인간 노화 및 장수 관련 유전자의 정제된 데이터베이스를 살펴볼 수 있습니다. 이름·기능 클러스터·선정 기준(예: GWAS, 모델 생물 수명 연장)·GO 용어·발현 변화 방향으로 유전자를 검색하고, 노화의 특징(hallmarks of aging)·여러 생물종에서의 수명 효과·관련 질환을 포함한 전체 유전자 레코드를 확인할 수 있습니다. 노화 생물학에 특화되어 있습니다.",
    "tools": [
      {
        "name": "search_genes",
        "desc": "OpenGenes 데이터베이스에서 노화 관련 유전자를 찾을 수 있습니다. 질환·발현 변화 방향·선정 기준·노화 기전·단백질 분류·유전자 심볼로 좁혀 요약된 유전자 레코드를 살펴볼 수 있는 주요 탐색 도구입니다."
      },
      {
        "name": "get_gene_by_id",
        "desc": "OpenGenes 숫자 ID로 특정 노화 관련 유전자의 전체 레코드를 확인할 수 있습니다. 노화의 특징·질환·연구 항목·수명 효과·단백질 분류를 포함한 완전한 데이터를 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_by_symbol",
        "desc": "HGNC 유전자 심볼(예: \"TP53\", \"FOXO3\", \"SIRT1\")로 특정 노화 관련 유전자의 전체 레코드를 확인할 수 있습니다. 기능·질환·노화 기전·노화의 특징·수명 연구·연구 참고문헌을 포함한 완전한 데이터를 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_by_ncbi_id",
        "desc": "NCBI Entrez Gene ID(숫자)로 특정 노화 관련 유전자의 전체 레코드를 확인할 수 있습니다. get_gene_by_symbol과 동일한 완전한 데이터를 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_suggestions",
        "desc": "부분 입력 문자열을 기반으로 유전자 심볼 및 이름의 자동완성 제안을 확인할 수 있습니다. 일치하는 유전자 심볼 목록을 살펴보며 올바른 유전자 심볼을 파악할 때 활용합니다."
      },
      {
        "name": "get_gene_symbols",
        "desc": "현재 OpenGenes 데이터베이스에 있는 모든 유전자 심볼을 확인할 수 있습니다. HGNC 유전자 심볼의 전체 목록을 살펴보며 사용 가능한 모든 유전자를 열거하거나 조회용 목록을 구성할 때 활용합니다."
      },
      {
        "name": "get_latest_genes",
        "desc": "OpenGenes에 가장 최근 추가된 유전자를 추가일 순(최신순)으로 확인할 수 있습니다. 요약된 유전자 레코드를 살펴보며 데이터베이스 업데이트를 추적하거나 새로 정제된 노화 유전자를 파악할 때 활용합니다."
      },
      {
        "name": "get_genes_by_functional_cluster",
        "desc": "하나 이상의 기능 클러스터에 속한 노화 관련 유전자를 확인할 수 있습니다. 기능 클러스터는 노화에서의 공통 생물학적 기능(예: DNA 복구, 미토콘드리아 기능)으로 유전자를 그룹화합니다."
      },
      {
        "name": "get_genes_by_selection_criteria",
        "desc": "하나 이상의 정제 선정 기준(예: 모델 생물에서의 수명 연장, GWAS 장수 연관성, 칼로리 제한 반응)에 일치하는 노화 관련 유전자를 확인할 수 있습니다."
      },
      {
        "name": "get_genes_by_go_term",
        "desc": "특정 유전자 온톨로지(GO) 용어로 주석된 노화 관련 유전자를 확인할 수 있습니다. GO 용어 이름 또는 ID로 일치시켜, 노화와 관련된 특정 생물학적 과정·분자 기능·세포 구성요소에 관여하는 유전자를 살펴볼 수 있습니다."
      },
      {
        "name": "get_genes_by_expression_change",
        "desc": "연령에 따른 발현 변화 방향으로 노화 관련 유전자를 확인할 수 있습니다. 연령에 따라 상향 조절되는 유전자는 \"1\", 하향 조절되는 유전자는 \"-1\", 발현 변화가 없는 유전자는 \"0\"으로 살펴볼 수 있습니다."
      },
      {
        "name": "get_gene_taxon",
        "desc": "노화 유전자에 대한 분류 및 분류 체계 메타데이터를 확인할 수 있습니다. 사용 가능한 기능 클러스터 ID·선정 기준 ID·관련 분류 데이터를 살펴보며 유효한 ID를 파악할 때 활용합니다."
      },
      {
        "name": "get_genes_increase_lifespan",
        "desc": "하나 이상의 모델 생물에서 수명 연장 증거가 있는 노화 관련 유전자를 확인할 수 있습니다(예: C. elegans, 초파리, 마우스에서 과발현 또는 녹아웃으로 수명이 연장된 경우). 요약된 유전자 레코드를 살펴볼 수 있습니다."
      },
      {
        "name": "get_model_organisms",
        "desc": "OpenGenes에서 노화 연구에 사용된 모델 생물 목록을 확인할 수 있습니다(예: C. elegans, Drosophila melanogaster, Mus musculus). 생물 ID·이름·라틴어 학명·분류군·일반적인 수명을 살펴볼 수 있습니다."
      },
      {
        "name": "get_phylums",
        "desc": "OpenGenes 모델 생물 데이터에 표현된 생물학적 문(phylum) 목록을 확인할 수 있습니다. 문 ID·이름·라틴어 학명을 살펴보며 데이터베이스의 분류학적 범위를 파악할 때 활용합니다."
      },
      {
        "name": "get_protein_classes",
        "desc": "OpenGenes에 정의된 모든 단백질 분류를 확인할 수 있습니다(예: 키나아제, 전사인자, 수용체). 단백질 분류 ID와 이름을 살펴볼 수 있습니다."
      },
      {
        "name": "get_diseases",
        "desc": "OpenGenes에서 노화 유전자와 관련된 질환의 전체 목록을 확인할 수 있습니다. 질환 ID·이름·ICD 코드를 살펴볼 수 있습니다."
      },
      {
        "name": "get_disease_categories",
        "desc": "OpenGenes에서 사용되는 질환 범주(ICD 챕터 수준 그룹화) 목록을 확인할 수 있습니다. 범주 ID와 ICD 코드를 살펴볼 수 있습니다."
      },
      {
        "name": "get_calorie_experiments",
        "desc": "OpenGenes의 칼로리 제한 및 식이 중재 실험을 확인할 수 있습니다. 각 레코드에서 생물·식이 유형·수명 변화(절대값 및 백분율)·실험 조건을 살펴볼 수 있어 가장 강한 효과를 파악할 때 활용합니다."
      },
      {
        "name": "get_aging_mechanisms",
        "desc": "OpenGenes에 정의된 노화 기전의 전체 목록을 확인할 수 있습니다(예: 텔로미어 손실, 유전체 불안정성, 후성유전적 변화, 미토콘드리아 기능장애). 기전 ID·이름·UUID를 살펴볼 수 있습니다."
      }
    ]
  },
  {
    "sys": "opentargets-server",
    "nameKo": "Open Targets",
    "desc": "Open Targets Platform 기반의 타깃-질환 연관성 및 타깃 약물성(tractability) 데이터베이스입니다. (1) 질환 기반 타깃 우선순위화(특정 질환과 가장 강하게 연관된 유전자 탐색), (2) 타깃 기반 질환 매핑(특정 유전자가 연관된 질환 탐색), (3) 타깃 약물성 평가(저분자·항체·PROTAC 등으로 약물화 가능한지 평가)에 활용합니다. ID 체계로 타깃은 Ensembl 유전자 ID(ENSG...), 질환은 EFO/MONDO ID(EFO_...·MONDO_...)를 사용하며, 연관성 점수(0–1)는 유전적·임상적·체세포 변이·문헌 증거를 통합한 값입니다. 화합물 구조·생물활성 데이터·임상시험 세부정보는 제공하지 않으므로 해당 정보는 ChEMBL을 사용합니다.",
    "tools": [
      {
        "name": "search_targets",
        "desc": "유전자 이름이나 심볼(예: \"EGFR\", \"TP53\", \"BRCA1\")로 유전자 타깃을 검색해 해당하는 Ensembl ID·이름·설명을 확인할 수 있습니다. 유전자 이름만 알고 있을 때 후속 조회에 필요한 Ensembl 유전자 ID(ENSG...)를 찾는 데 활용합니다."
      },
      {
        "name": "search_diseases",
        "desc": "질환 이름(예: \"lung cancer\", \"type 2 diabetes\", \"multiple myeloma\")으로 질환을 검색해 해당하는 EFO/MONDO ID·이름·설명을 확인할 수 있습니다. 질환 이름만 알고 있을 때 후속 조회에 필요한 EFO 질환 ID(EFO_...·MONDO_...)를 찾는 데 활용합니다."
      },
      {
        "name": "get_target_disease_associations",
        "desc": "타깃과 질환 간 양방향 연관성을 확인할 수 있습니다. 타깃 ID를 입력하면 그 타깃과 연관된 질환 목록을, 질환 ID를 입력하면 그 질환과 연관된 타깃 목록을 순위·점수와 함께 조회할 수 있으며, 연관성 점수는 0–1 범위로 높을수록 강한 증거를 의미합니다."
      },
      {
        "name": "get_disease_targets_summary",
        "desc": "질환 EFO ID를 기준으로 유전적·임상적·체세포 변이·문헌 증거를 통합한 전체 연관성 점수(0–1)가 높은 상위 타깃을 확인할 수 있습니다. 연관 타깃의 총 개수와 각 타깃의 데이터 유형별 점수도 함께 살펴볼 수 있어, 질환과 가장 강하게 연관된 타깃을 우선순위화할 때 활용합니다."
      },
      {
        "name": "get_target_details",
        "desc": "Ensembl ID로 특정 유전자 타깃의 생물학적·약물성 정보를 확인할 수 있습니다. 단백질의 분자 기전 설명(functionDescriptions)·유전자 별칭 및 대체 명칭(synonyms)·모달리티별 약물성 증거(tractability — 저분자·항체·PROTAC·기타 임상 모달리티)·유전자 biotype(예: protein_coding)을 살펴볼 수 있습니다."
      },
      {
        "name": "get_disease_details",
        "desc": "EFO/MONDO ID로 특정 질환의 기본 메타데이터를 확인할 수 있습니다. 질환 이름과 임상적 설명을 조회할 수 있어, EFO ID가 의도한 질환과 일치하는지 확인하거나 표준 설명을 파악할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "pdb-server",
    "nameKo": "PDB",
    "desc": "RCSB Protein Data Bank(PDB) 기반으로 실험적으로 규명된 단백질·핵산·복합체의 3D 구조를 제공합니다. 키워드 또는 UniProt accession으로 구조를 검색하고, 구조 메타데이터(해상도·측정 방법·생물종)를 조회하며, 좌표 파일을 내려받고 구조 품질을 평가합니다. PDB ID는 4자리 영숫자 코드이며(예: 6VXX), 예측 구조는 AlphaFold를 사용합니다.",
    "tools": [
      {
        "name": "search_structures",
        "desc": "키워드·단백질 이름 또는 PDB ID로 RCSB PDB에서 실험적으로 규명된 구조를 검색할 수 있습니다. 일치하는 PDB ID와 관련도 점수·매칭된 서비스 유형을 확인할 수 있습니다."
      },
      {
        "name": "get_structure_info",
        "desc": "4자리 PDB ID로 특정 PDB 엔트리의 상세 메타데이터를 확인할 수 있습니다. 제목·실험 방법·해상도·분자량·폴리머 조성·공간군(space group)·단위 격자(unit cell)·등록/공개일·품질 지표(clashscore·Ramachandran outliers·R-factor)·대표 인용문헌을 살펴볼 수 있습니다."
      },
      {
        "name": "search_by_uniprot",
        "desc": "UniProt accession과 연관된 모든 PDB 구조를 확인할 수 있습니다. 일치하는 PDB ID와 관련도 점수를 조회할 수 있어, 단백질 서열(UniProt)을 실험적으로 규명된 구조에 매핑할 때 활용합니다."
      },
      {
        "name": "get_structure_quality",
        "desc": "PDB 엔트리의 구조 품질 및 검증 지표를 확인할 수 있습니다. 해상도(Å)·R-work/R-free factor·clashscore·Ramachandran outliers(%)·rotamer outliers(%)·bond/angle RMSZ를 살펴볼 수 있어, 후속 분석 전에 실험 품질을 평가할 때 활용합니다."
      }
    ]
  },
  {
    "sys": "proteinatlas-server",
    "nameKo": "Protein Atlas",
    "desc": "Human Protein Atlas 기반으로 20,000개 이상의 인간 단백질을 다루는 단백질 발현 데이터베이스입니다. 자유 텍스트 유전자/키워드 검색, 조직 특이적 발현 프로파일링(RNA/IHC), 세포내 위치(subcellular localization), 혈액 및 뇌 영역별 발현, TCGA 암종별 예후 데이터, 항체 신뢰도 정보를 지원합니다. 조직/세포 발현 프로파일링, 세포내 위치 조회, 암 예후 마커 발굴에 사용합니다.",
    "tools": [
      {
        "name": "search_proteins",
        "desc": "유전자 심볼·단백질 이름 또는 자유 텍스트 키워드로 Human Protein Atlas 엔트리를 검색할 수 있습니다. 유전자 이름·동의어·설명에 대한 전문 검색을 통해 단백질 요약 정보(유전자 심볼·Ensembl ID·설명·UniProt·염색체·단백질 클래스·증거 수준)를 확인할 수 있어, Protein Atlas 탐색의 일반적인 진입점으로 활용합니다."
      },
      {
        "name": "get_protein_info",
        "desc": "단일 유전자 심볼에 대한 상세 Human Protein Atlas 엔트리를 확인할 수 있습니다. 이미 유전자를 알고 있고 해당 핵심 Protein Atlas 레코드를 살펴볼 때 활용합니다."
      },
      {
        "name": "get_protein_by_ensembl",
        "desc": "Ensembl 유전자 식별자로 Human Protein Atlas 엔트리를 확인할 수 있습니다. 유전자 심볼 대신 Ensembl ID로 단백질 레코드를 조회할 때 활용합니다."
      },
      {
        "name": "get_tissue_expression",
        "desc": "단일 유전자에 대해 주요 인간 조직 전반의 조직 수준 RNA 발현 데이터를 확인할 수 있습니다."
      },
      {
        "name": "search_by_tissue",
        "desc": "특정 조직과 연관된 단백질을 확인할 수 있으며, 발현 수준으로 필터링할 수도 있습니다. 조직 특이적으로 풍부하게 발현되는 마커를 발굴할 때 활용합니다."
      },
      {
        "name": "get_blood_expression",
        "desc": "단일 유전자에 대해 주요 혈구 유형 전반의 혈구 특이적 발현 데이터를 확인할 수 있습니다."
      },
      {
        "name": "get_brain_expression",
        "desc": "단일 유전자에 대해 주요 뇌 영역 전반의 뇌 영역 특이적 발현 데이터를 확인할 수 있습니다."
      },
      {
        "name": "get_subcellular_location",
        "desc": "단일 단백질의 세포내 위치(subcellular localization) 주석과 해당 세포 구획 정보를 확인할 수 있습니다."
      },
      {
        "name": "search_by_subcellular_location",
        "desc": "특정 세포내 구획과 연관된 단백질을 확인할 수 있으며, ICC(면역형광) 위치 신뢰도로 필터링할 수도 있습니다."
      },
      {
        "name": "get_pathology_data",
        "desc": "단일 유전자에 대한 암 병리 및 예후 주석을 Human Protein Atlas 병리 데이터에서 확인할 수 있습니다."
      },
      {
        "name": "search_cancer_markers",
        "desc": "암종 및/또는 예후 방향과 연관된 단백질을 검색할 수 있습니다. Protein Atlas 병리 데이터로부터 후보 암 마커를 발굴할 때 활용합니다."
      },
      {
        "name": "get_antibody_info",
        "desc": "단일 단백질에 대한 항체 검증·신뢰도·염색 관련 정보를 확인할 수 있습니다."
      },
      {
        "name": "advanced_search",
        "desc": "여러 필터(자유 텍스트·조직 연관성·세포내 위치·TCGA 암종 예후·단백질 클래스·염색체·항체 신뢰도)를 조합해 조건에 맞는 단백질을 검색할 수 있습니다. 결과에는 단백질별로 TCGA 암종 예후 정보가 포함되어 예후 유형(favorable/unfavorable)·예후 분류(potential/validated/unprognostic)·유의성(p-value)을 함께 확인할 수 있어, favorable과 unfavorable 예후 마커를 구분할 때 활용합니다."
      },
      {
        "name": "batch_protein_lookup",
        "desc": "여러 유전자 심볼에 대한 Protein Atlas 엔트리를 한 번에 확인할 수 있습니다. 동일한 유형의 핵심 조회를 여러 유전자에 대해 반복할 때 활용합니다."
      },
      {
        "name": "compare_expression_profiles",
        "desc": "여러 유전자에 걸쳐 조직·뇌·혈액 또는 단일세포 발현 관점을 비교할 수 있습니다. 소규모 유전자 세트의 상대적 발현 패턴을 살펴볼 때 활용합니다."
      },
      {
        "name": "get_protein_classes",
        "desc": "단일 유전자에 대한 단백질 클래스 및 기능 주석을 분류 형식의 요약과 함께 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "pubchem-server",
    "nameKo": "PubChem",
    "desc": "PubChem 기반으로 1억 개 이상의 화합물을 다루는 NCBI 화학 데이터베이스입니다. 이름·SMILES·InChI·CAS 번호로 검색하고, 화합물 특성(분자량·분자식·LogP·TPSA)을 조회하며, 유사 화합물 또는 부분구조 매칭을 찾고, 3D 형태(conformer)를 얻고, 생물활성 데이터에 접근합니다. PubChem Compound ID(CID)는 정수이며, 무료로 이용 가능한 가장 포괄적인 화학 데이터베이스입니다.",
    "tools": [
      {
        "name": "search_compounds",
        "desc": "화합물 이름·CAS 번호·분자식·SMILES·InChI 또는 CID로 PubChem에서 화합물을 검색할 수 있습니다. 주요 특성(MW·SMILES·IUPAC 이름)과 함께 CID를 확인할 수 있습니다. 자연어·타깃 이름·회사명 검색에는 적합하지 않으며, 그런 경우 ChEMBL이나 OpenTargets를 활용합니다."
      },
      {
        "name": "get_compound_info",
        "desc": "PubChem CID로 화합물의 주요 분자 특성을 확인할 수 있습니다. 분자식·MW·SMILES·InChI/Key·IUPAC 이름·XLogP·TPSA·수소결합 주개/받개 수·회전 가능 결합 수·중원자 수·complexity를 살펴볼 수 있습니다."
      },
      {
        "name": "search_by_smiles",
        "desc": "정확히 일치하는 SMILES로 PubChem CID와 주요 특성을 확인할 수 있습니다. 유사도 기반의 모호 검색에는 search_similar_compounds를 활용합니다."
      },
      {
        "name": "search_by_inchi",
        "desc": "InChI 문자열 또는 InChIKey로 PubChem CID와 주요 특성을 확인할 수 있습니다. 입력 유형은 자동으로 감지됩니다."
      },
      {
        "name": "search_by_cas_number",
        "desc": "CAS 등록번호(예: 아스피린은 50-78-2)로 PubChem CID와 주요 특성을 확인할 수 있습니다."
      },
      {
        "name": "get_compound_synonyms",
        "desc": "화합물에 등록된 이름과 동의어(상품명·일반명·CAS 번호 등)를 확인할 수 있습니다."
      },
      {
        "name": "search_similar_compounds",
        "desc": "Tanimoto 지문(fingerprint) 유사도를 사용해 구조적으로 유사한 화합물을 검색할 수 있습니다. 조건에 맞는 화합물의 CID를 확인할 수 있어, 유사 구조 탐색에 활용합니다."
      },
      {
        "name": "substructure_search",
        "desc": "주어진 부분구조(쿼리 SMILES)를 포함하는 화합물을 검색할 수 있습니다. 일치하는 화합물의 CID를 확인할 수 있어, 스캐폴드 기반 검색에 활용합니다."
      },
      {
        "name": "superstructure_search",
        "desc": "쿼리의 상위구조(superstructure)인 화합물, 즉 쿼리가 그 안에 포함된 화합물을 검색할 수 있습니다. 일치하는 화합물의 CID를 확인할 수 있어, 유도체 및 확장된 유사체를 찾을 때 활용합니다."
      },
      {
        "name": "get_3d_conformers",
        "desc": "화합물의 3D 형태(conformer) 통계를 확인할 수 있습니다. 3D 부피와 이용 가능한 conformer 수를 살펴볼 수 있습니다."
      },
      {
        "name": "analyze_stereochemistry",
        "desc": "화합물의 입체화학을 확인할 수 있습니다. 전체/정의된 원자 입체중심(stereocenter)·전체/정의된 결합 입체중심·isomeric SMILES를 살펴볼 수 있어, 카이랄성 및 E/Z 이성질체를 파악할 때 활용합니다."
      },
      {
        "name": "get_compound_properties",
        "desc": "화합물의 물리화학적 특성을 확인할 수 있습니다. MW·XLogP·TPSA·수소결합 주개/받개 수·회전 가능 결합 수·중원자 수·complexity·전하를 살펴볼 수 있어, ADMET 스크리닝 및 선도물질 최적화에 활용합니다."
      },
      {
        "name": "calculate_descriptors",
        "desc": "화합물의 포괄적인 2D/3D 분자 기술자(descriptor)를 확인할 수 있습니다. 분자식·MW·XLogP·TPSA·complexity·수소결합 수·회전 가능 결합 수·중원자 수·입체중심·3D 부피·conformer 수를 살펴볼 수 있어, 케모인포매틱스 및 ML 특징 생성에 활용합니다."
      },
      {
        "name": "assess_drug_likeness",
        "desc": "Lipinski의 Rule of Five(MW≤500·XLogP≤5·HBD≤5·HBA≤10)와 Veber 규칙(회전 가능 결합 수≤10·TPSA≤140 Å²)을 사용해 경구 약물성(drug-likeness)을 확인할 수 있습니다. pass/fail 판정·개별 위반 항목·약물성 점수(0–1)를 살펴볼 수 있습니다."
      },
      {
        "name": "analyze_molecular_complexity",
        "desc": "PubChem의 complexity 점수·중원자 수·회전 가능 결합 수·입체중심을 바탕으로 분자 복잡도를 확인할 수 있습니다. 복잡도가 low/medium/high/very high로 분류되며 합성 접근성(synthetic accessibility) 가이드를 함께 살펴볼 수 있습니다."
      },
      {
        "name": "get_assay_info",
        "desc": "AID로 PubChem 생물검정(bioassay)의 요약 정보를 확인할 수 있습니다. 이름·유형·타깃·출처·활성 건수를 살펴볼 수 있습니다."
      },
      {
        "name": "get_compound_bioactivities",
        "desc": "화합물의 생물검정 결과를 확인할 수 있습니다. AID·검정 이름·활성 결과(outcome)·활성 값을 살펴볼 수 있으며, 결과(active/inactive/inconclusive)로 필터링할 수도 있습니다."
      },
      {
        "name": "compare_activity_profiles",
        "desc": "여러 화합물에 걸쳐 물리화학적 특성과 기본 생물활성 데이터를 비교할 수 있습니다. 주요 기술자를 나란히 비교한 표를 확인할 수 있습니다."
      },
      {
        "name": "get_safety_data",
        "desc": "화합물의 GHS(국제 통합 시스템) 위험 분류 및 안전 데이터를 확인할 수 있습니다. 신호어(signal word)·위험 문구(hazard statement)·그림문자(pictogram) 코드를 살펴볼 수 있습니다."
      },
      {
        "name": "get_external_references",
        "desc": "화합물의 교차 데이터베이스 식별자를 확인할 수 있습니다. ChEMBL·DrugBank·KEGG·FDA·HMDB 등의 ID를 살펴볼 수 있어, 외부 데이터베이스 연계에 활용합니다."
      },
      {
        "name": "search_patents",
        "desc": "화합물과 연관된 특허 ID(예: US20040132771)를 확인할 수 있습니다. IP 환경(landscape) 분석에 활용합니다."
      },
      {
        "name": "get_literature_references",
        "desc": "화합물을 인용하거나 기술하는 문헌의 PubMed ID를 확인할 수 있습니다. 문헌 분석 및 SAR(구조-활성 관계) 연구에 활용합니다."
      }
    ]
  },
  {
    "sys": "reactome-server",
    "nameKo": "Reactome",
    "desc": "큐레이션된 인간 경로(pathway) 데이터베이스인 Reactome용 MCP 서버입니다. 키워드·유전자 또는 질환으로 경로를 검색하고, 경로 상세정보(GO 용어·문헌·질환 연관성)를 조회하며, 경로 계층 구조를 탐색하고, 생화학 반응 및 참여 분자를 나열하고, 경로 맥락에서 단백질과 이벤트를 살펴보는 도구를 제공합니다. 모든 데이터는 Reactome Content Service(reactome.org)에서 가져오며, 경로 ID는 R-HSA-XXXXXX 형식을 사용합니다.",
    "tools": [
      {
        "name": "search_pathways",
        "desc": "이름·요약·식별자 전반의 전문 검색을 통해 Reactome에서 경로·반응·단백질 또는 복합체를 키워드로 검색할 수 있습니다. 일치 항목의 안정 ID·이름·유형·생물종·요약 텍스트를 확인할 수 있습니다."
      },
      {
        "name": "get_pathway_details",
        "desc": "안정 식별자 또는 이름으로 Reactome 경로의 종합 상세정보를 확인할 수 있습니다. 표시 이름·스키마 유형·생물종·질환 연관성 및 질환 이름·compartment·요약 텍스트·GO 생물학적 과정 용어·문헌 참조·하위 이벤트를 살펴볼 수 있습니다. 경로 ID는 R-HSA-XXXXXX(인간) 또는 R-MMU-XXXXXX(마우스) 형식을 따릅니다."
      },
      {
        "name": "find_pathways_by_gene",
        "desc": "특정 유전자 또는 단백질을 참여자로 포함하는 모든 Reactome 경로를 확인할 수 있습니다. 유전자 심볼 또는 UniProt accession으로 검색해 해당 단백질이 등장하는 경로의 ID·이름·생물종을 조회할 수 있습니다."
      },
      {
        "name": "find_pathways_by_disease",
        "desc": "질환 이름 또는 키워드와 연관된 경로를 확인할 수 있습니다. 암·감염병·대사 질환·신경퇴행·유전 질환을 다루는 Reactome 큐레이션 질환 경로로부터 경로 ID·이름·생물종·짧은 요약을 살펴볼 수 있습니다."
      },
      {
        "name": "get_pathway_hierarchy",
        "desc": "생물학적 계층 구조에서 Reactome 경로의 위치를 확인할 수 있습니다. 최상위까지의 전체 조상 사슬(상위 경로)과 직접 하위 이벤트를 살펴볼 수 있어 경로의 맥락을 파악할 때 활용합니다(예: \"MAPK signaling\"은 \"Signal Transduction\" → \"Signaling by Receptor Tyrosine Kinases\"의 하위 경로)."
      },
      {
        "name": "get_pathway_participants",
        "desc": "Reactome 경로에 참여하는 물리적 엔티티(단백질·복합체·저분자·RNA)를 확인할 수 있습니다. 엔티티 ID·표시 이름·스키마 유형·생물종·분자 식별자(단백질은 UniProt, 저분자는 ChEBI)를 살펴볼 수 있어, 경로의 분자적 구성을 파악할 때 활용합니다."
      },
      {
        "name": "get_pathway_reactions",
        "desc": "Reactome 경로에 포함된 생화학 반응 및 이벤트(Reaction·BlackBoxEvent)를 확인할 수 있습니다. 반응 ID·이름·스키마 유형·가역성·생물종을 살펴볼 수 있으며, BlackBoxEvent는 충분히 규명되지 않은 과정을 나타냅니다."
      },
      {
        "name": "get_protein_interactions",
        "desc": "Reactome 경로의 참여 단백질/엔티티 및 생화학 반응을 확인할 수 있으며, 상호작용 유형(단백질-단백질·촉매·조절·전체)으로 필터링할 수도 있습니다. Reactome은 이진 PPI 대신 다분자 생화학 반응(입력·출력·촉매·조절자)을 모델링하므로, 전용 이진 PPI 데이터에는 STRING-db 또는 IntAct를 활용합니다."
      }
    ]
  },
  {
    "sys": "string-server",
    "nameKo": "STRING",
    "desc": "STRING 기반으로 14,000종 이상의 생물에서 6,700만 개 이상의 단백질을 다루는 단백질-단백질 상호작용(PPI) 데이터베이스입니다. 신뢰도 점수가 포함된 상호작용 네트워크를 조회하고, 기능 농축 분석(GO·KEGG·Reactome)을 수행하며, 상동체(homolog)를 찾고 단백질을 주석합니다. 상호작용 출처에는 실험 데이터·공발현(co-expression)·텍스트 마이닝·데이터베이스 전이(database transfer)가 포함되며, PPI 네트워크 분석 및 경로 맥락 파악에 사용합니다.",
    "tools": [
      {
        "name": "get_protein_interactions",
        "desc": "특정 단백질의 직접 상호작용 파트너를 확인할 수 있습니다. 결합 신뢰도 점수(0-1000)와 개별 증거 채널 점수(neighborhood·gene fusion·계통발생적 공출현·공발현·experimental·큐레이션된 데이터베이스·텍스트 마이닝)를 함께 살펴볼 수 있으며, 신뢰도 점수는 ≥ 400이 중간·≥ 700이 높음·≥ 900이 최고 신뢰도입니다."
      },
      {
        "name": "get_interaction_network",
        "desc": "단백질 세트에 대한 단백질-단백질 상호작용(PPI) 네트워크를 구축해 살펴볼 수 있습니다. 네트워크 노드(단백질 식별자·주석·크기)와 신뢰도 점수·증거 유형이 포함된 상호작용 엣지를 확인할 수 있으며, 상위 상호작용 단백질을 추가해 네트워크를 확장하거나(add_nodes) 직접 결합만(physical) 또는 모든 기능적 연관성(functional)으로 범위를 선택할 수 있습니다."
      },
      {
        "name": "get_functional_enrichment",
        "desc": "단백질 세트에 대해 STRING의 배경 프로테옴을 기준으로 기능 농축 분석(과대표현 분석)을 수행할 수 있습니다. 여러 주석 데이터베이스(Gene Ontology(생물학적 과정·분자 기능·세포 구성요소)·KEGG·Reactome·UniProt 키워드·PFAM·SMART·InterPro)에 걸친 농축 용어를 관측 유전자 수·배경 수·p-value·FDR 보정 p-value(Benjamini-Hochberg)와 함께 확인할 수 있습니다."
      },
      {
        "name": "get_protein_annotations",
        "desc": "단백질 목록에 대한 STRING 식별자와 기능 주석을 확인할 수 있습니다. 각 입력 식별자에 대해 변환된 STRING ID·대표 유전자 이름·NCBI 분류군(taxonomy) ID·단백질 크기(아미노산 수)·기능 주석 텍스트를 살펴볼 수 있어, 식별자 검증이나 유전자 심볼을 STRING ID로 변환할 때 활용합니다."
      },
      {
        "name": "find_homologs",
        "desc": "양방향 최적 매칭(bidirectional best-hit) 오솔로지(orthology)를 사용해 다른 생물종에 걸친 쿼리 단백질의 상동(homologous) 단백질을 확인할 수 있습니다. STRING ID·대표 이름·기능 주석을 생물종별로 그룹화해 살펴볼 수 있으며, 특정 대상 생물종으로 범위를 제한할 수도 있습니다."
      },
      {
        "name": "search_proteins",
        "desc": "유전자 이름·단백질 이름 또는 식별자로 STRING 데이터베이스에서 단백질을 검색해 STRING ID로 변환할 수 있습니다. 일치하는 단백질의 STRING ID·대표 유전자 이름·NCBI 분류군 ID·단백질 크기·주석 설명을 확인할 수 있어, 상호작용 조회 전 올바른 STRING 식별자를 파악할 때 활용합니다. 특정 생물종으로 범위를 제한할 수도 있습니다."
      }
    ]
  },
  {
    "sys": "sureChembl-server",
    "nameKo": "SureChEMBL",
    "desc": "SureChEMBL — EMBL-EBI의 특허 화학 데이터베이스입니다. USPTO·EPO·WIPO·JPO 특허의 전문(full-text) 마이닝으로 추출한 화합물을 다루며, 특허 문서 속 화합물 검색, 특허 내용·연관 화합물 조회, 화합물–특허 관계 탐색을 지원합니다. 특허 선행기술(prior art) 조사와 자유실시(FTO) 분석에 사용합니다.",
    "tools": [
      {
        "name": "get_document_content",
        "desc": "특정 특허 문서의 전체 내용을 확인할 수 있습니다. 문서 ID(예: EP1234567)를 입력하면 해당 특허의 제목·초록·출원인·발명자·IPC 분류·우선권 정보·상세 설명·청구항까지 전문을 살펴볼 수 있습니다."
      },
      {
        "name": "get_patent_family",
        "desc": "하나의 특허가 여러 국가·관할권(USPTO·EPO·WIPO·JPO 등)에 출원된 동등 특허(패밀리)를 확인할 수 있습니다. 같은 발명이 어느 나라에 출원·등록되어 있는지 파악할 때 활용합니다."
      },
      {
        "name": "search_chemicals_by_name",
        "desc": "화합물 이름으로 SureChEMBL에 등록된 화합물을 찾을 수 있습니다. 해당 화합물의 SureChEMBL ID·구조(SMILES)와 함께 어떤 특허에 등장하는지 확인할 수 있습니다."
      },
      {
        "name": "get_chemical_by_id",
        "desc": "SureChEMBL 화합물 ID로 특정 화합물의 상세 정보를 확인할 수 있습니다. 구조(SMILES·InChI)·분자 특성과 해당 화합물이 등장하는 특허를 살펴볼 수 있습니다."
      },
      {
        "name": "search_by_smiles",
        "desc": "SMILES 구조로 정확히 일치하는 화합물을 찾을 수 있습니다. 해당 화합물의 ID·특성과 그 화합물이 등장하는 특허를 확인할 수 있어, 특정 구조와 특허의 연결 관계를 파악할 때 활용합니다."
      },
      {
        "name": "get_chemical_image",
        "desc": "화합물의 SMILES 또는 InChI로 2D 화학 구조 이미지를 생성해 볼 수 있습니다. 구조를 시각적으로 확인하거나 보고서 자료로 활용할 때 사용합니다."
      },
      {
        "name": "search_structure",
        "desc": "화학 구조를 기준으로 화합물을 검색할 수 있습니다(부분구조 포함·구조 유사·정확히 일치 등 다양한 방식). 조건에 맞는 화합물과 그 화합물이 등장하는 특허를 확인할 수 있습니다."
      }
    ]
  },
  {
    "sys": "nedrug-patent",
    "nameKo": "NeDrug-Patent",
    "desc": "식품의약품안전처(MFDS)가 공공데이터포털(data.go.kr)을 통해 제공하는 의약품 특허 OpenAPI입니다. 국내 의약품 특허정보(성분명·제품명·허가업체·특허등록번호·존속기간 만료일)와 국내 제품에 연계된 FDA 오렌지북 특허정보(성분명·제품명·출원/등록번호·발명의 명칭·출원인·만료일·특허상태)를 조회할 수 있습니다. 신약 개발 일정 수립, 특허 만료 시점 파악, 특허 침해 회피 검토에 활용합니다.",
    "tools": [
      {
        "name": "search_kr_drug_patents",
        "desc": "MFDS 국내 의약품 특허정보 데이터베이스를 조회할 수 있습니다. 특정 의약품을 보호하는 특허와 그 만료 시점을 확인하여 개발 일정 수립 및 특허 침해 회피에 활용합니다. 각 결과에서 성분명·제품명·허가업체·특허등록번호·특허 존속기간 만료일을 확인할 수 있습니다."
      },
      {
        "name": "search_orangebook_patents",
        "desc": "MFDS가 제공하는 FDA 오렌지북 특허정보 데이터베이스를 조회할 수 있습니다. 미국 FDA 오렌지북 특허를 국내 의약품 제품과 연계하여, 특정 성분의 한·미 특허 보호 현황을 비교하고 특허 만료 후 출시 시점을 계획할 때 활용합니다. 각 결과에서 성분명·제품명·출원번호·등록번호·발명의 명칭·출원인·만료일·특허상태를 확인할 수 있습니다."
      }
    ]
  }
]

export const MCP_SERVER_TOTAL = MCP_SERVERS.length
