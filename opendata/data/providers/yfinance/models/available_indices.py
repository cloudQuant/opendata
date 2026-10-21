"""Yahoo Finance available-index directory (local reference table, no HTTP)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from opendata.data.capability import Capability
from opendata.data.models.yfinance_available_indices import YFinanceAvailableIndices
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.yfinance._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

INDICES = {
    "amex": {"name": "NYSE-AMEX Composite Index", "ticker": "^XAX"},
    "ar_mervel": {"name": "S&P MERVAL TR Index (USD)", "ticker": "M.BA"},
    "arca_btk": {"name": "NYSE ARCA Biotech Index", "ticker": "^BKT"},
    "arca_drg": {"name": "NYSE ARCA Pharmaceutical Index", "ticker": "^DRG"},
    "arca_hui": {"name": "NYSE ARCA Gold Bugs Index", "ticker": "^HUI"},
    "arca_ixb": {"name": "NYSE Materials Select Sector Index", "ticker": "^IXB"},
    "arca_mxy": {"name": "NYSE ARCA Mexico Index (USD)", "ticker": "^MXY"},
    "arca_nwx": {"name": "NYSE ARCA Networking Index", "ticker": "^NWX"},
    "arca_pse": {"name": "NYSE ARCA Tech 100 Index", "ticker": "^PSE"},
    "arca_xal": {"name": "NYSE ARCA Airline Index", "ticker": "^XAL"},
    "arca_xbd": {"name": "NYSE ARCA Securities Broker/Dealer Index", "ticker": "^XBD"},
    "arca_xci": {"name": "NYSE ARCA Computer Tech Index", "ticker": "^XCI"},
    "arca_xii": {"name": "NYSE ARCA Institutional Index", "ticker": "^XII"},
    "arca_xmi": {"name": "NYSE ARCA Major Market Index", "ticker": "^XMI"},
    "arca_xng": {"name": "NYSE ARCA Natural Gas Index", "ticker": "^XNG"},
    "arca_xoi": {"name": "NYSE ARCA Oil and Gas Index", "ticker": "^XOI"},
    "arca_xtc": {"name": "NYSE ARCA N.A. Telecom Industry Index", "ticker": "^XTC"},
    "at_atx": {"name": "Wiener Börse Austrian 20 Index (EUR)", "ticker": "^ATX"},
    "at_atx5": {"name": "Vienna ATX Five Index (EUR)", "ticker": "^ATX5"},
    "at_prime": {"name": "Vienna ATX Prime Index (EUR)", "ticker": "^ATXPRIME"},
    "au_aord": {"name": "Australia All Ordinary Share Index (AUD)", "ticker": "^AORD"},
    "au_asx100": {"name": "S&P/ASX 100 Index (AUD)", "ticker": "^ATOI"},
    "au_asx20": {"name": "S&P/ASX 20 Index (AUD)", "ticker": "^ATLI"},
    "au_asx200": {"name": "S&P/ASX 200 Index (AUD)", "ticker": "^AXJO"},
    "au_asx300": {"name": "S&P/ASX 300 Index (AUD)", "ticker": "^AXKO"},
    "au_asx50": {"name": "S&P/ASX 50 Index (AUD)", "ticker": "^AFLI"},
    "au_asx50_mid": {"name": "S&P/ASX Mid Cap 50 Index (AUD)", "ticker": "^AXMD"},
    "au_communications": {
        "name": "S&P/ASX 200 Communications Sector Index (AUD)",
        "ticker": "^AXTJ",
    },
    "au_discretionary": {
        "name": "S&P/ASX 200 Consumer Discretionary Sector Index (AUD)",
        "ticker": "^AXDJ",
    },
    "au_energy": {"name": "S&P/ASX 200 Energy Sector Index (AUD)", "ticker": "^AXEJ"},
    "au_financials": {"name": "S&P/ASX 200 Financials Sector Index (AUD)", "ticker": "^AXFJ"},
    "au_health": {"name": "S&P/ASX 200 Health Care Sector Index (AUD)", "ticker": "^AXHJ"},
    "au_industrials": {"name": "S&P/ASX 200 Industrials Sector Index (AUD)", "ticker": "^AXNJ"},
    "au_materials": {"name": "S&P/ASX 200 Materials Sector Index (AUD)", "ticker": "^AXMJ"},
    "au_mining": {"name": "S&P/ASX 300 Metals and Mining Sector Index (AUD)", "ticker": "^AXMM"},
    "au_reit": {"name": "S&P/ASX 200 A-REIT Industry Index (AUD)", "ticker": "^AXPJ"},
    "au_resources": {"name": "S&P/ASX 200 Resources Sector Index (AUD)", "ticker": "^AXJR"},
    "au_small": {"name": "S&P/ASX Small Ordinaries Index (AUD)", "ticker": "^AXSO"},
    "au_staples": {"name": "S&P/ASX 200 Consumer Staples Sector Index (AUD)", "ticker": "^AXSJ"},
    "au_tech": {"name": "S&P/ASX 200 Info Tech Sector Index (AUD)", "ticker": "^AXIJ"},
    "au_utilities": {"name": "S&P/ASX 200 Utilities Sector Index (AUD)", "ticker": "^AXUJ"},
    "be_bel20": {"name": "BEL 20 Brussels Index (EUR)", "ticker": "^BFX"},
    "br_bvsp": {"name": "IBOVESPA Sao Paulo Brazil Index (BRL)", "ticker": "^BVSP"},
    "br_ivbx": {"name": "IVBX2 Indice Valour (BRL)", "ticker": "^IVBX"},
    "ca_banks": {"name": "S&P/TSX Composite Banks Index (CAD)", "ticker": "TXBA.TS"},
    "ca_tsx": {"name": "TSX Composite Index (CAD)", "ticker": "^GSPTSE"},
    "cboe_bxm": {"name": "CBOE Buy-Write Monthly Index", "ticker": "^BXM"},
    "cboe_bxr": {"name": "CBOE Russell 2000 Buy-Write Index", "ticker": "^BXR"},
    "cboe_bxrt": {"name": "CBOE Russell 2000 30-Delta Buy-Write Index", "ticker": "^BXRT"},
    "cboe_evz": {"name": "CBOE Euro Currency Volatility Index", "ticker": "^EVZ"},
    "cboe_gvz": {"name": "CBOE Gold Volatility Index", "ticker": "^GVZ"},
    "cboe_irx": {"name": "CBOE 13 Week Treasury Bill", "ticker": "^IRX"},
    "cboe_ovx": {"name": "CBOE Crude Oil Volatility Index", "ticker": "^OVX"},
    "cboe_rvx": {"name": "CBOE Russell 2000 Volatility Index", "ticker": "^RVX"},
    "cboe_shortvol": {"name": "CBOE Short VIX Futures Index", "ticker": "^SHORTVOL"},
    "cboe_skew": {"name": "CBOE Skew Index", "ticker": "^SKEW"},
    "cboe_tnx": {"name": "CBOE Interest Rate 10 Year T-Note", "ticker": "^TNX"},
    "cboe_tyx": {"name": "CBOE 30 year Treasury Yields", "ticker": "^TYX"},
    "cboe_vin": {"name": "CBOE Near-Term VIX Index", "ticker": "^VIN"},
    "cboe_vix": {"name": "CBOE S&P 500 Volatility Index", "ticker": "^VIX"},
    "cboe_vix3m": {"name": "CBOE S&P 500 3-Month Volatility Index", "ticker": "^VIX3M"},
    "cboe_vix9d": {"name": "CBOE S&P 500 9-Day Volatility Index", "ticker": "^VIX9D"},
    "cboe_vvix": {"name": "CBOE VIX Volatility Index", "ticker": "^VVIX"},
    "cboe_vxn": {"name": "CBOE NASDAQ 100 Volatility Index", "ticker": "^VXN"},
    "ch_stoxx": {"name": "Zurich STXE 600 PR Index (EUR)", "ticker": "^STOXX"},
    "ch_stoxx50e": {"name": "Zurich ESTX 50 PR Index (EUR)", "ticker": "^STOXX50E"},
    "cn_csi300": {"name": "China CSI 300 Index (CNY)", "ticker": "000300.SS"},
    "cn_sse_a": {"name": "SSE A Share Index (CNY)", "ticker": "000002.SS"},
    "cn_sse_comp": {"name": "SSE Composite Index (CNY)", "ticker": "000001.SS"},
    "cn_szse_a": {"name": "SZSE A-Shares Index (CNY)", "ticker": "399107.SZ"},
    "cn_szse_comp": {"name": "SZSE Component Index (CNY)", "ticker": "399001.SZ"},
    "crypto200": {"name": "CMC Crypto 200 Index by Solacti", "ticker": "^CMC200"},
    "de_dax40": {"name": "DAX Performance Index (EUR)", "ticker": "^GDAXI"},
    "de_mdax60": {"name": "DAX Mid Cap Performance Index (EUR)", "ticker": "^MDAXI"},
    "de_sdax70": {"name": "DAX Small Cap Performance Index (EUR)", "ticker": "^SDAXI"},
    "de_tecdax30": {"name": "DAX Tech Sector TR Index (EUR)", "ticker": "^TECDAX"},
    "dk_benchmark": {"name": "OMX Copenhagen Benchmark GI (DKK)", "ticker": "^OMXCBGI"},
    "dk_omxc25": {"name": "OMX Copenhagen 25 Index (DKK)", "ticker": "^OMXC25"},
    "dow_dja": {"name": "Dow Jones Composite Average Index", "ticker": "^DJA"},
    "dow_dji": {"name": "Dow Jones Industrial Average Index", "ticker": "^DJI"},
    "dow_djt": {"name": "Dow Jones Transportation Average Index", "ticker": "^DJT"},
    "dow_dju": {"name": "Dow Jones Utility Average Index", "ticker": "^DJU"},
    "dow_djus": {"name": "Dow Jones US Index", "ticker": "^DJUS"},
    "dow_rci": {"name": "Dow Jones Composite All REIT Index", "ticker": "^RCI"},
    "dxy": {"name": "US Dollar Index", "ticker": "DX-Y.NYB"},
    "es_ibex35": {"name": "IBEX 35 - Madrid CATS (EUR)", "ticker": "^IBEX"},
    "eu_fteu1": {"name": "FTSE Eurotop 100 Index (EUR)", "ticker": "^FTEU1"},
    "eu_n100": {"name": "Euronext 100 Index (EUR)", "ticker": "^N100"},
    "eu_speup": {"name": "S&P Europe 350 Index (EUR)", "ticker": "^SPEUP"},
    "fi_omxh25": {"name": "OMX Helsinki 25 (EUR)", "ticker": "^OMXH25"},
    "fr_cac40": {"name": "CAC 40 PR Index (EUR)", "ticker": "^FCHI"},
    "fr_next20": {"name": "CAC Next 20 Index (EUR)", "ticker": "^CN20"},
    "ftai": {"name": "FTSE AIM All-Share Global Index (GBP)", "ticker": "^FTAI"},
    "ftse100": {"name": "FTSE Global 100 Index (GBP)", "ticker": "^FTSE"},
    "ftse250": {"name": "FTSE Global 250 Index (GBP)", "ticker": "^FTMC"},
    "ftse350": {"name": "FTSE Global 350 Index (GBP)", "ticker": "^FTLC"},
    "hk_china": {"name": "Hang Seng China-Affiliated Corporations Index (HKD)", "ticker": "^HSCC"},
    "hk_finance": {"name": "Hang Seng Finance Sector Index (HKD)", "ticker": "^HSNF"},
    "hk_hko": {"name": "NYSE ARCA Hong Kong Options Index (USD)", "ticker": "^HKO"},
    "hk_hsi": {"name": "Hang Seng Index (HKD)", "ticker": "^HSI"},
    "hk_properties": {"name": "Hang Seng Properties Sector Index (HKD)", "ticker": "^HSNP"},
    "hk_titans30": {"name": "Dow Jones Hong Kong Titans 30 Index (HKD)", "ticker": "^XLHK"},
    "hk_utilities": {"name": "Hang Seng Utilities Sector Index (HKD)", "ticker": "^HSNU"},
    "ice_auto": {"name": "ICE FactSet Global NextGen Auto Index", "ticker": "^ICEFSNA"},
    "ice_comm": {"name": "ICE FactSet Global NextGen Communications Index", "ticker": "^ICEFSNC"},
    "id_jkse": {"name": "Jakarta Composite Index (IDR)", "ticker": "^JKSE"},
    "id_lq45": {"name": "Indonesia Stock Exchange LQ45 Index (IDR)", "ticker": "^JKLQ45"},
    "ie_iseq": {"name": "ISEQ Irish All Shares Index (EUR)", "ticker": "^ISEQ"},
    "il_ta125": {"name": "Tel-Aviv 125 Index (ILS)", "ticker": "^TA125.TA"},
    "in_bse": {"name": "S&P Bombay SENSEX (INR)", "ticker": "^BSESN"},
    "in_bse100": {"name": "S&P BSE 100 Index (INR)", "ticker": "BSE-100.BO"},
    "in_bse200": {"name": "S&P BSE 200 Index (INR)", "ticker": "BSE-200.BO"},
    "in_bse500": {"name": "S&P BSE 500 Index (INR)", "ticker": "BSE-500.BO"},
    "in_bse_mcap": {"name": "S&P Bombay Mid Cap Index (INR)", "ticker": "BSE-MIDCAP.BO"},
    "in_bse_scap": {"name": "S&P Bombay Small Cap Index (INR)", "ticker": "BSE-SMLCAP.BO"},
    "in_nse50": {"name": "NSE Nifty 50 Index (INR)", "ticker": "^NSEI"},
    "in_nse500": {"name": "NSE Nifty 500 Index (INR)", "ticker": "^CRSLDX"},
    "in_nse_bank": {"name": "NSE Nifty Bank Industry Index (INR)", "ticker": "^NSEBANK"},
    "in_nse_mcap": {"name": "NSE Nifty 50 Mid Cap Index (INR)", "ticker": "^NSEMDCP50"},
    "it_mib40": {"name": "FTSE MIB 40 Index (EUR)", "ticker": "FTSEMIB.MI"},
    "jp_arca": {"name": "NYSE ARCA Japan Index (JPY)", "ticker": "^JPN"},
    "jp_auto": {"name": "Nikkei 500 Auto & Auto Parts Index (JPY)", "ticker": "^NG17.OS"},
    "jp_fintech": {"name": "Global Fintech Japan Hedged Index (JPY)", "ticker": "^FDSFTPRJPY"},
    "jp_n225": {"name": "Nikkei 255 Index (JPY)", "ticker": "^N225"},
    "jp_n300": {"name": "Nikkei 300 Index (JPY)", "ticker": "^N300"},
    "jp_ndiv": {"name": "Nikkei Average Double Inverse Index (JPY)", "ticker": "^NDIV.OS"},
    "jp_nkcc": {"name": "Nikkei Avg Covered Call Index (JPY)", "ticker": "^NKCC.OS"},
    "jp_nkdh": {"name": "Nikkei Average USD Hedge Index (JPY)", "ticker": "^NKDH.OS"},
    "jp_nkeh": {"name": "Nikkei Average EUR Hedge Index (JPY)", "ticker": "^NKEH.OS"},
    "jp_nkhd": {"name": "Nikkei Avg High Dividend Yield Index (JPY)", "ticker": "^NKHD.OS"},
    "jp_nklv": {"name": "Nikkei Avg Leverage Index (JPY)", "ticker": "^NKLV.OS"},
    "jp_nknr": {"name": "Nikkei Avg Net TR Index (JPY)", "ticker": "^NKVI.OS"},
    "jp_nkrc": {"name": "Nikkei Avg Risk Control Index (JPY)", "ticker": "^NKRC.OS"},
    "kr_kospi": {"name": "KOSPI Composite Index (KRW)", "ticker": "^KS11"},
    "move": {"name": "ICE BofAML Move Index", "ticker": "^MOVE"},
    "mx_ipc": {"name": "IPC Mexico Index (MXN)", "ticker": "^MXX"},
    "my_klci": {"name": "FTSE Kuala Lumpur Composite Index (MYR)", "ticker": "^KLSE"},
    "nasdaq": {"name": "Nasdaq Composite Index", "ticker": "^IXIC"},
    "nasdaq100": {"name": "NASDAQ 100", "ticker": "^NDX"},
    "nasdaq100_ew": {"name": "NASDAQ 100 Equal Weighted Index", "ticker": "^NDXE"},
    "nasdaq50": {"name": "NASDAQ Q50 Index", "ticker": "^NXTQ"},
    "nl_aex": {"name": "Euronext Dutch 25 Index (EUR)", "ticker": "^AEX"},
    "nl_amx": {"name": "Euronext Dutch Mid Cap Index (EUR)", "ticker": "^AMX"},
    "nq_bank": {"name": "NASDAQ Bank Index", "ticker": "^BANK"},
    "nq_bkx": {"name": "KBW NASDAQ Bank Index", "ticker": "^BKX"},
    "nq_ex_tech": {"name": "NASDAQ 100 Ex-Tech Sector Index", "ticker": "^NDXX"},
    "nq_inds": {"name": "NASDAQ Industrial Index", "ticker": "^INDS"},
    "nq_ixch": {"name": "NASDAQ Health Care Index", "ticker": "^IXCH"},
    "nq_ixco": {"name": "NASDAQ Computer Index", "ticker": "^INCO"},
    "nq_ixtc": {"name": "NASDAQ Telecommunications Index", "ticker": "^IXTC"},
    "nq_kix": {"name": "KBW NASDAQ Insurance Index", "ticker": "^KIX"},
    "nq_krx": {"name": "KBW NASDAQ Regional Bank Index", "ticker": "^KRX"},
    "nq_ksx": {"name": "KBW NASDAQ Capital Markets Index", "ticker": "^KSX"},
    "nq_nbi": {"name": "NASDAQ Biotech Index", "ticker": "^NBI"},
    "nq_tech": {"name": "NASDAQ 100 Technology Sector Index", "ticker": "^NDXT"},
    "nq_tran": {"name": "NASDAQ Transportation Index", "ticker": "^TRAN"},
    "nyse": {"name": "NYSE Composite Index", "ticker": "^NYA"},
    "nyse_fang": {"name": "NYSE FANG+TM index", "ticker": "^NYFANG"},
    "nyse_ny": {"name": "NYSE US 100 Index", "ticker": "^NY"},
    "nyse_nyi": {"name": "NYSE International 100 Index", "ticker": "^NYI"},
    "nyse_nyl": {"name": "NYSE World Leaders Index", "ticker": "^NYL"},
    "nyse_nyy": {"name": "NYSE TMT Index", "ticker": "^NYY"},
    "nz50": {"name": "S&P New Zealand 50 Index (NZD)", "ticker": "^nz50"},
    "nz_small": {"name": "S&P/NZX Small Cap Index (NZD)", "ticker": "^NZSC"},
    "ph_psei": {"name": "Philippine Stock Exchange Index (PHP)", "ticker": "PSEI.PS"},
    "phlx_hgx": {"name": "PHLX Housing Sector Index", "ticker": "^HGX"},
    "phlx_osx": {"name": "PHLX Oil Services Sector Index", "ticker": "^OSX"},
    "phlx_sox": {"name": "PHLX Semiconductor Index", "ticker": "^SOX"},
    "phlx_uty": {"name": "PHLX Utility Sector Index", "ticker": "^UTY"},
    "phlx_xau": {"name": "PHLX Gold/Silver Index", "ticker": "^XAU"},
    "pt_bvlg": {"name": "Lisbon PSI All-Share Index GR (EUR)", "ticker": "^BVLG"},
    "reit_fnar": {"name": "FTSE Nareit All Equity REITs Index", "ticker": "^FNAR"},
    "reit_wgreit": {"name": "Wilshire Global REIT Index", "ticker": "^WGREIT"},
    "reit_wgresi": {"name": "Wilshire Global Real Estate Sector Index", "ticker": "^WGRESI"},
    "reit_wilreit": {"name": "Wilshire US REIT Index", "ticker": "^WILREIT"},
    "reit_wilresi": {"name": "Wilshire US Real Estate Security Index", "ticker": "^WILRESI"},
    "ru_moex": {"name": "MOEX Russia Index (RUB)", "ticker": "IMOEX.ME"},
    "russell1000": {"name": "Russell 1000 Index", "ticker": "^RUI"},
    "russell2000": {"name": "Russell 2000 Index", "ticker": "^RUT"},
    "russell3000": {"name": "Russell 3000 Index", "ticker": "^RUA"},
    "russellgrowth": {"name": "Russell 2000 Growth Index", "ticker": "^RUO"},
    "russellvalue": {"name": "Russell 2000 Value Index", "ticker": "^RUJ"},
    "se_benchmark": {"name": "OMX Stockholm Benchmark GI (SEK)", "ticker": "^OMXSBGI"},
    "se_omx30": {"name": "OMX Stockholm 30 Index (SEK)", "ticker": "^OMX"},
    "se_omxspi": {"name": "OMX Stockholm All Share PI (SEK)", "ticker": "^OMXSPI"},
    "sg_sti": {"name": "STI Singapore Index (SGD)", "ticker": "^STI"},
    "sp400": {"name": "S&P 400 Mid Cap Index", "ticker": "^SP400"},
    "sp500": {"name": "S&P 500 Index", "ticker": "^GSPC"},
    "sp500tr": {"name": "S&P 500 TR Index", "ticker": "^SP500TR"},
    "sp600": {"name": "S&P 600 Small Cap Index", "ticker": "^SP600"},
    "sp_communications_sector": {
        "name": "S&P 500 Communications Sector Index",
        "ticker": "^SP500-50",
    },
    "sp_communications_telecom": {
        "name": "S&P 500 Diversified Telecommunications Services Industry Index",
        "ticker": "^SP500-501010",
    },
    "sp_discretionary_auto_components": {
        "name": "S&P 500 Auto Components Industry Index",
        "ticker": "^SP500-251010",
    },
    "sp_discretionary_autos": {
        "name": "S&P 500 Automobiles Industry Index",
        "ticker": "^SP500-251020",
    },
    "sp_discretionary_autos-ig": {
        "name": "S&P 500 Automobiles and Components (Industry Group) Index",
        "ticker": "^SP500-2510",
    },
    "sp_discretionary_durables_household": {
        "name": "S&P 500 Household Durables Industry Index",
        "ticker": "^SP500-252010",
    },
    "sp_discretionary_durables_ig": {
        "name": "S&P 500 Consumer Durables & Apparel (Industry Group) Index",
        "ticker": "^SP500-2520",
    },
    "sp_discretionary_leisure": {
        "name": "S&P 500 Leisure Products Industry Index",
        "ticker": "^SP500-252020",
    },
    "sp_discretionary_sector": {
        "name": "S&P 500 Consumer Discretionary Index",
        "ticker": "^SP500-25",
    },
    "sp_discretionary_services_consumer": {
        "name": "S&P 500 Consumer Services (Industry Group) Index",
        "ticker": "^SP500-2530",
    },
    "sp_discretionary_textiles": {
        "name": "S&P 500 Textiles, Apparel & Luxury Goods Industry Index",
        "ticker": "^SP500-252030",
    },
    "sp_energy_equipment": {
        "name": "S&P 500 Energy Equipment & Services Industry Index",
        "ticker": "^SP500-101010",
    },
    "sp_energy_ig": {"name": "S&P 500 Energy (Industry Group) Index", "ticker": "^SP500-1010"},
    "sp_energy_oil": {
        "name": "S&P 500 Oil, Gas & Consumable Fuels Industry Index",
        "ticker": "^SP500-101020",
    },
    "sp_financials_capital": {
        "name": "S&P 500 Capital Markets Industry Index",
        "ticker": "^SP500-402030",
    },
    "sp_financials_consumer": {
        "name": "S&P 500 Consumer Finance Industry Index",
        "ticker": "^SP500-402020",
    },
    "sp_financials_diversified_ig": {
        "name": "S&P 500 Diversified Financials (Industry Group) Index",
        "ticker": "^SP500-4020",
    },
    "sp_financials_sector": {"name": "S&P 500 Financials Sector Index", "ticker": "^SP500-40"},
    "sp_financials_services": {
        "name": "S&P 500 Diversified Financial Services Industry Index",
        "ticker": "^SP500-402010",
    },
    "sp_health_biotech": {
        "name": "S&P 500 Biotechnology Industry Index",
        "ticker": "^SP500-352010",
    },
    "sp_health_equipment": {
        "name": "S&P 500 Health Care Equipment & Services (Industry Group) Index",
        "ticker": "^SP500-3510",
    },
    "sp_health_pharma": {
        "name": "S&P 500 Pharmaceuticals Industry Index",
        "ticker": "^SP500-352020",
    },
    "sp_health_providers": {
        "name": "S&P 500 Health Care Providers & Services Industry Index",
        "ticker": "^SP500-351020",
    },
    "sp_health_sciences": {
        "name": "S&P 500 Pharmaceuticals, Biotechnology & Life Sciences (Industry Group) Index",
        "ticker": "^SP500-3520",
    },
    "sp_health_sector": {"name": "S&P 500 Health Care Sector Index", "ticker": "^SP500-35"},
    "sp_health_supplies": {
        "name": "S&P 500 Health Care Equipment & Supplies Industry Index",
        "ticker": "^SP500-351010",
    },
    "sp_industrials_aerospace": {
        "name": "S&P 500 Aerospace & Defense Industry Index",
        "ticker": "^SP500-201010",
    },
    "sp_industrials_building": {
        "name": "S&P 500 Building Products Industry Index",
        "ticker": "^SP500-201020",
    },
    "sp_industrials_conglomerates": {
        "name": "S&P 500 Industrial Conglomerates Industry Index",
        "ticker": "^SP500-201050",
    },
    "sp_industrials_construction": {
        "name": "S&P 500 Construction & Engineering Industry Index",
        "ticker": "^SP500-201030",
    },
    "sp_industrials_distributors": {
        "name": "S&P 500 Trading Companies & Distributors Industry Index",
        "ticker": "^SP500-201070",
    },
    "sp_industrials_electrical": {
        "name": "S&P 500 Electrical Equipment Industry Index",
        "ticker": "^SP500-201040",
    },
    "sp_industrials_goods_ig": {
        "name": "S&P 500 Capital Goods (Industry Group) Index",
        "ticker": "^SP500-2010",
    },
    "sp_industrials_machinery": {
        "name": "S&P 500 Machinery Industry Index",
        "ticker": "^SP500-201060",
    },
    "sp_industrials_sector": {"name": "S&P 500 Industrials Sector Index", "ticker": "^SP500-20"},
    "sp_industrials_services_ig": {
        "name": "S&P 500 Commercial & Professional Services (Industry Group) Index",
        "ticker": "^SP500-2020",
    },
    "sp_industrials_services_supplies": {
        "name": "S&P 500 Commercial Services & Supplies Industry Index",
        "ticker": "^SP500-202010",
    },
    "sp_industrials_transport_air": {
        "name": "S&P 500 Air Freight & Logistics Industry",
        "ticker": "^SP500-203010",
    },
    "sp_industrials_transport_airlines": {
        "name": "S&P 500 Airlines Industry Index",
        "ticker": "^SP500-203020",
    },
    "sp_industrials_transport_ground": {
        "name": "S&P 500 Road & Rail Industry Index",
        "ticker": "^SP500-203040",
    },
    "sp_industrials_transport_ig": {
        "name": "S&P 500 Transportation (Industry Group) Index",
        "ticker": "^SP500-2030",
    },
    "sp_it_hardware": {
        "name": "S&P 500 Technology Hardware Equipment (Industry Group) Index",
        "ticker": "^SP500-4520",
    },
    "sp_it_saas_ig": {
        "name": "S&P 500 Software and Services (Industry Group) Index",
        "ticker": "^SP500-4510",
    },
    "sp_it_sector": {"name": "S&P 500 IT Sector Index", "ticker": "^SP500-45"},
    "sp_it_semi": {
        "name": "S&P 500 Semiconductor & Semiconductor Equipment Industry",
        "ticker": "^SP500-453010",
    },
    "sp_it_software": {"name": "S&P 500 Software Industry Index", "ticker": "^SP500-451030"},
    "sp_materials_construction": {
        "name": "S&P 500 Construction Materials Industry Index",
        "ticker": "^SP500-151020",
    },
    "sp_materials_ig": {
        "name": "S&P 500 Materials (Industry Group) Index",
        "ticker": "^SP500-1510",
    },
    "sp_materials_metals": {
        "name": "S&P 500 Mining & Metals Industry Index",
        "ticker": "^SP500-151040",
    },
    "sp_materials_sector": {"name": "S&P 500 Materials Sector Index", "ticker": "^SP500-15"},
    "sp_re_ig": {"name": "S&P 500 Real Estate (Industry Group) Index", "ticker": "^SP500-6010"},
    "sp_re_sector": {"name": "S&P 500 Real Estate Sector Index", "ticker": "^SP500-60"},
    "sp_staples_beverages": {"name": "S&P 500 Beverages Industry Index", "ticker": "^SP500-302010"},
    "sp_staples_food_ig": {
        "name": "S&P 500 Food Beverage & Tobacco (Industry Group) Index",
        "ticker": "^SP500-3020",
    },
    "sp_staples_household_ig": {
        "name": "S&P 500 Household & Personal Products (Industry Group) Index",
        "ticker": "^SP500-3030",
    },
    "sp_staples_products_food": {
        "name": "S&P 500 Food Products Industry Index",
        "ticker": "^SP500-302020",
    },
    "sp_staples_products_household": {
        "name": "S&P 500 Household Products Industry Index",
        "ticker": "^SP500-303010",
    },
    "sp_staples_products_personal": {
        "name": "S&P 500 Personal Products Industry Index",
        "ticker": "^SP500-303020",
    },
    "sp_staples_retail_ig": {
        "name": "S&P 500 Food & Staples Retailing (Industry Group) Index",
        "ticker": "^SP500-3010",
    },
    "sp_staples_sector": {"name": "S&P 500 Consumer Staples Sector Index", "ticker": "^SP500-30"},
    "sp_staples_tobacco": {"name": "S&P 500 Tobacco Industry Index", "ticker": "^SP500-302030"},
    "sp_utilities_electricity": {
        "name": "S&P 500 Electric Utilities Index",
        "ticker": "^SP500-551010",
    },
    "sp_utilities_multis": {
        "name": "S&P 500 Multi-Utilities Industry Index",
        "ticker": "^SP500-551030",
    },
    "sp_utilities_sector": {"name": "S&P 500 Utilities Sector Index", "ticker": "^SP500-55"},
    "sp_xsp": {"name": "S&P 500 Mini SPX Options Index", "ticker": "^XSP"},
    "sphyda": {"name": "S&P High Yield Aristocrats Index", "ticker": "^SPHYDA"},
    "spx": {"name": "S&P 500 Index", "ticker": "^SPX"},
    "th_set": {"name": "Thailand SET Index (THB)", "ticker": "^SET.BK"},
    "tw_tcii": {"name": "TSEC Cement and Ceramics Subindex (TWD)", "ticker": "^TCII"},
    "tw_tfii": {"name": "TSEC Foods Subindex (TWD)", "ticker": "^TFII"},
    "tw_tfni": {"name": "TSEC Finance Subindex (TWD)", "ticker": "^TFNI"},
    "tw_tpai": {"name": "TSEC Paper and Pulp Subindex (TWD)", "ticker": "^TPAI"},
    "tw_twii": {"name": "TSEC Weighted Index (TWD)", "ticker": "^TWII"},
    "uk_100": {"name": "CBOE UK 100 Index (GBP)", "ticker": "^BUK100P"},
    "uk_ftas": {"name": "UK FTSE All-Share Index (GBP)", "ticker": "^FTAS"},
    "uk_spuk": {"name": "S&P United Kingdom Index (PDS)", "ticker": "^SPUK"},
    "w5000": {"name": "Wilshire 5000", "ticker": "^W5000"},
    "w5000flt": {"name": "Wilshire 5000 Float Adjusted Index", "ticker": "^W5000FLT"},
    "w5klcg": {"name": "Wilshire US Large Cap Growth Index", "ticker": "^W5KLCG"},
    "w5klcv": {"name": "Wilshire US Large Cap Value Index", "ticker": "^W5KLCV"},
    "za_jo": {"name": "Johannesburg All Share Index (ZAR)", "ticker": "^J203.JO"},
    "za_jo_altex": {"name": "Johannesburg Alt Exchange Index (ZAR)", "ticker": "^J232.JO"},
    "za_jo_mcap": {"name": "Johannesburg Large and Mid Cap Index (ZAR)", "ticker": "^J206.JO"},
    "za_shariah": {"name": "Johannesburg Shariah All Share Index (ZAR)", "ticker": "^J143.JO"},
}


class YFinanceAvailableIndicesQuery(QueryParams):
    """The fixed local directory takes no filter."""

    source: Literal["yfinance"] = "yfinance"
    market: Literal["global"] = "global"


class YFinanceAvailableIndicesFetcher(Fetcher[YFinanceAvailableIndicesQuery, dict[str, Any]]):
    """Read the reference table; the offline witness supplies the same shape."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "AvailableIndices"
    row_model: ClassVar[type[YFinanceAvailableIndices]] = YFinanceAvailableIndices
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="yfinance_available_indices",
        period="na",
        market="global",
        source="yfinance",
        verified=False,
        notes=(
            "The upstream model serves a local reference table (no HTTP endpoint); the live path "
            "reads that table and the offline witness supplies the same shape."
        ),
    )

    def transform_query(self, **kwargs: object) -> YFinanceAvailableIndicesQuery:
        """The directory takes no parameters."""
        return YFinanceAvailableIndicesQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: YFinanceAvailableIndicesQuery,
        ctx: FetchContext,
    ) -> dict[str, Any]:
        """Use the injected reference table when present, otherwise the embedded one."""
        if self.http_transport is not None:
            response = self.http_transport(
                "reference://yfinance/available-indices",
                {},
                timeout=5.0,
                source=SOURCE,
                headers={},
            )
            document = response.document
        else:
            document = INDICES
        if not isinstance(document, dict) or not document:
            raise ProviderEngineError("YFINANCE_SHAPE_INVALID")
        return document

    def transform_data(
        self,
        raw: dict[str, Any],
        params: YFinanceAvailableIndicesQuery,
    ) -> FetchResult:
        """Transposed index table rows -> one contract row per index."""
        rows: list[YFinanceAvailableIndices] = []
        for code, entry in raw.items():
            if not isinstance(code, str) or not code.strip() or not isinstance(entry, dict):
                raise ProviderEngineError("YFINANCE_SHAPE_INVALID")
            name = entry.get("name")
            ticker = entry.get("ticker")
            if not isinstance(name, str) or not name.strip():
                raise ProviderEngineError("YFINANCE_SHAPE_INVALID")
            if not isinstance(ticker, str) or not ticker.strip():
                raise ProviderEngineError("YFINANCE_SHAPE_INVALID")
            rows.append(YFinanceAvailableIndices(code=code, name=name, symbol=ticker))
        return tuple(rows)


__all__ = ["YFinanceAvailableIndicesFetcher", "YFinanceAvailableIndicesQuery"]
