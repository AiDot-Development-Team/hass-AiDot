"""Constants for the aidot integration."""

DOMAIN = "aidot"

CONF_USERNAME = "server_username"
CONF_PASSWORD = "server_password"
CONF_SERVER_COUNTRY = "server_country"

CONF_CHOOSE_HOUSE = "choose_house"

CLOUD_SERVERS = [
    {"_id": "1-0", "id": "AL", "name": "Albania", "ext": "", "region": "EU"},
    {
        "_id": "1-1",
        "id": "AG",
        "name": "Antigua and Barbuda",
        "ext": "",
        "region": "US",
    },
    {"_id": "1-2", "id": "AR", "name": "Argentina", "ext": "", "region": "US"},
    {"_id": "1-3", "id": "AT", "name": "Austria", "ext": "", "region": "EU"},
    {"_id": "1-4", "id": "AI", "name": "Anguilla", "ext": "", "region": "US"},
    {"_id": "1-5", "id": "AF", "name": "Afghanistan", "ext": "", "region": "JP"},
    {"_id": "1-6", "id": "AM", "name": "Armenia", "ext": "", "region": "JP"},
    {"_id": "1-7", "id": "AZ", "name": "Azerbaijan", "ext": "", "region": "JP"},
    {"_id": "1-8", "id": "AU", "name": "Australia", "ext": "", "region": "JP"},
    {"_id": "2-0", "id": "BO", "name": "Bolivia", "ext": "", "region": "US"},
    {"_id": "2-1", "id": "BR", "name": "Brazil", "ext": "", "region": "US"},
    {"_id": "2-2", "id": "BG", "name": "Bulgaria", "ext": "", "region": "EU"},
    {"_id": "2-3", "id": "BS", "name": "Bahamas", "ext": "", "region": "US"},
    {"_id": "2-4", "id": "BE", "name": "Belgium", "ext": "", "region": "EU"},
    {"_id": "2-5", "id": "BZ", "name": "Belize", "ext": "", "region": "US"},
    {"_id": "2-6", "id": "BB", "name": "Barbados", "ext": "", "region": "US"},
    {"_id": "2-7", "id": "BM", "name": "Bermuda", "ext": "", "region": "US"},
    {"_id": "2-8", "id": "BN", "name": "Brunei", "ext": "", "region": "JP"},
    {"_id": "2-9", "id": "BH", "name": "Bahrain", "ext": "", "region": "JP"},
    {"_id": "2-10", "id": "BD", "name": "Bangladesh", "ext": "", "region": "JP"},
    {"_id": "2-11", "id": "BT", "name": "Bhutan", "ext": "", "region": "JP"},
    {"_id": "3-0", "id": "CA", "name": "Canada", "ext": "", "region": "US"},
    {"_id": "3-1", "id": "CL", "name": "Chile", "ext": "", "region": "US"},
    {"_id": "3-2", "id": "CO", "name": "Colombia", "ext": "", "region": "US"},
    {"_id": "3-3", "id": "CR", "name": "Costa Rica", "ext": "", "region": "US"},
    {"_id": "3-4", "id": "CU", "name": "Cuba", "ext": "", "region": "US"},
    {"_id": "3-5", "id": "CZ", "name": "Czech Republic", "ext": "", "region": "EU"},
    {"_id": "3-6", "id": "HR", "name": "Croatia", "ext": "", "region": "EU"},
    {"_id": "3-7", "id": "KY", "name": "Cayman Islands", "ext": "", "region": "US"},
    {"_id": "3-8", "id": "KH", "name": "Cambodia", "ext": "", "region": "JP"},
    {"_id": "3-9", "id": "CY", "name": "Cyprus", "ext": "", "region": "JP"},
    {"_id": "4-0", "id": "DK", "name": "Denmark", "ext": "", "region": "EU"},
    {"_id": "4-1", "id": "DO", "name": "Dominican Republic", "ext": "", "region": "US"},
    {"_id": "5-0", "id": "EC", "name": "Ecuador", "ext": "", "region": "US"},
    {"_id": "5-1", "id": "SV", "name": "El Salvador", "ext": "", "region": "US"},
    {"_id": "5-2", "id": "EE", "name": "Estonia", "ext": "", "region": "EU"},
    {"_id": "6-0", "id": "FI", "name": "Finland", "ext": "", "region": "EU"},
    {"_id": "6-1", "id": "FR", "name": "France", "ext": "", "region": "EU"},
    {"_id": "6-2", "id": "GF", "name": "French Guiana", "ext": "", "region": "US"},
    {"_id": "7-0", "id": "GR", "name": "Greece", "ext": "", "region": "EU"},
    {"_id": "7-1", "id": "GT", "name": "Guatemala", "ext": "", "region": "US"},
    {"_id": "7-2", "id": "GY", "name": "Guyana", "ext": "", "region": "US"},
    {"_id": "7-3", "id": "DE", "name": "Germany", "ext": "", "region": "EU"},
    {"_id": "7-4", "id": "GD", "name": "Grenada", "ext": "", "region": "US"},
    {"_id": "7-5", "id": "GE", "name": "Georgia", "ext": "", "region": "JP"},
    {"_id": "8-0", "id": "HT", "name": "Haiti", "ext": "", "region": "US"},
    {"_id": "8-1", "id": "HN", "name": "Honduras", "ext": "", "region": "US"},
    {"_id": "8-2", "id": "HU", "name": "Hungary", "ext": "", "region": "EU"},
    {"_id": "9-0", "id": "IS", "name": "Iceland", "ext": "", "region": "EU"},
    {"_id": "9-1", "id": "IE", "name": "Ireland", "ext": "", "region": "EU"},
    {"_id": "9-2", "id": "IT", "name": "Italy", "ext": "", "region": "EU"},
    {"_id": "9-3", "id": "IN", "name": "India", "ext": "", "region": "JP"},
    {"_id": "9-4", "id": "ID", "name": "Indonesia", "ext": "", "region": "JP"},
    {"_id": "9-5", "id": "IR", "name": "Iran", "ext": "", "region": "JP"},
    {"_id": "9-6", "id": "IQ", "name": "Iraq", "ext": "", "region": "JP"},
    {"_id": "9-7", "id": "IL", "name": "Israel", "ext": "", "region": "EU"},
    {"_id": "10-0", "id": "JM", "name": "Jamaica", "ext": "", "region": "US"},
    {"_id": "10-1", "id": "JP", "name": "Japan", "ext": "", "region": "JP"},
    {"_id": "10-2", "id": "JO", "name": "Jordan", "ext": "", "region": "JP"},
    {"_id": "11-0", "id": "KZ", "name": "Kazakhstan", "ext": "", "region": "JP"},
    {"_id": "11-1", "id": "KR", "name": "Korea", "ext": "", "region": "JP"},
    {"_id": "11-2", "id": "KW", "name": "Kuwait", "ext": "", "region": "JP"},
    {"_id": "11-3", "id": "KG", "name": "Kyrgyzstan", "ext": "", "region": "JP"},
    {"_id": "12-0", "id": "LV", "name": "Latvia", "ext": "", "region": "EU"},
    {"_id": "12-1", "id": "LT", "name": "Lithuania", "ext": "", "region": "EU"},
    {"_id": "12-2", "id": "LU", "name": "Luxembourg", "ext": "", "region": "EU"},
    {"_id": "12-3", "id": "LI", "name": "Liechtenstein", "ext": "", "region": "EU"},
    {"_id": "12-4", "id": "LA", "name": "Laos", "ext": "", "region": "JP"},
    {"_id": "12-5", "id": "LB", "name": "Lebanon", "ext": "", "region": "JP"},
    {"_id": "13-0", "id": "MT", "name": "Malta", "ext": "", "region": "EU"},
    {"_id": "13-1", "id": "MX", "name": "Mexico", "ext": "", "region": "US"},
    {"_id": "13-2", "id": "MD", "name": "Moldova", "ext": "", "region": "EU"},
    {"_id": "13-3", "id": "MC", "name": "Monaco", "ext": "", "region": "EU"},
    {"_id": "13-4", "id": "MS", "name": "Montserrat", "ext": "", "region": "US"},
    {"_id": "13-5", "id": "ME", "name": "Montenegro", "ext": "", "region": "EU"},
    {"_id": "13-6", "id": "MY", "name": "Malaysia", "ext": "", "region": "JP"},
    {"_id": "13-7", "id": "MV", "name": "Maldives", "ext": "", "region": "JP"},
    {"_id": "13-8", "id": "MN", "name": "Mongolia", "ext": "", "region": "JP"},
    {"_id": "13-9", "id": "MM", "name": "Myanmar", "ext": "", "region": "JP"},
    {"_id": "14-0", "id": "NL", "name": "Netherlands", "ext": "", "region": "EU"},
    {"_id": "14-1", "id": "NI", "name": "Nicaragua", "ext": "", "region": "US"},
    {"_id": "14-2", "id": "NO", "name": "Norway", "ext": "", "region": "EU"},
    {"_id": "14-3", "id": "MK", "name": "North Macedonia", "ext": "", "region": "EU"},
    {"_id": "14-4", "id": "NZ", "name": "New Zealand", "ext": "", "region": "JP"},
    {"_id": "14-5", "id": "NP", "name": "Nepal", "ext": "", "region": "JP"},
    {"_id": "14-6", "id": "KP", "name": "North Korea", "ext": "", "region": "JP"},
    {"_id": "15-0", "id": "OM", "name": "Oman", "ext": "", "region": "JP"},
    {"_id": "16-0", "id": "PA", "name": "Panama", "ext": "", "region": "US"},
    {"_id": "16-1", "id": "PY", "name": "Paraguay", "ext": "", "region": "US"},
    {"_id": "16-2", "id": "PE", "name": "Peru", "ext": "", "region": "US"},
    {"_id": "16-3", "id": "PT", "name": "Portugal", "ext": "", "region": "EU"},
    {"_id": "16-4", "id": "PK", "name": "Pakistan", "ext": "", "region": "JP"},
    {"_id": "16-5", "id": "PS", "name": "Palestine", "ext": "", "region": "JP"},
    {"_id": "16-6", "id": "PH", "name": "Philippines", "ext": "", "region": "JP"},
    {"_id": "17-0", "id": "QA", "name": "Qatar", "ext": "", "region": "JP"},
    {"_id": "18-0", "id": "RO", "name": "Romania", "ext": "", "region": "EU"},
    {"_id": "18-1", "id": "RU", "name": "Russia", "ext": "", "region": "EU"},
    {"_id": "19-0", "id": "SM", "name": "San Marino", "ext": "", "region": "EU"},
    {"_id": "19-1", "id": "SI", "name": "Slovenia", "ext": "", "region": "EU"},
    {"_id": "19-2", "id": "ES", "name": "Spain", "ext": "", "region": "EU"},
    {"_id": "19-3", "id": "LC", "name": "St.Lucia", "ext": "", "region": "US"},
    {"_id": "19-4", "id": "SR", "name": "Suriname", "ext": "", "region": "US"},
    {"_id": "19-5", "id": "SE", "name": "Sweden", "ext": "", "region": "EU"},
    {"_id": "19-6", "id": "SK", "name": "Slovakia", "ext": "", "region": "EU"},
    {"_id": "19-7", "id": "RS/ME", "name": "Serbia", "ext": "", "region": "EU"},
    {
        "_id": "19-8",
        "id": "KN",
        "name": "St.Kitts and Nevis",
        "ext": "",
        "region": "US",
    },
    {
        "_id": "19-9",
        "id": "VC",
        "name": "St.Vincent and the Grenadines",
        "ext": "",
        "region": "US",
    },
    {"_id": "19-10", "id": "SA", "name": "Saudi Arabia", "ext": "", "region": "JP"},
    {"_id": "19-11", "id": "SG", "name": "Singapore", "ext": "", "region": "JP"},
    {"_id": "19-12", "id": "LK", "name": "Sri Lanka", "ext": "", "region": "JP"},
    {"_id": "19-13", "id": "SY", "name": "Syria", "ext": "", "region": "JP"},
    {"_id": "19-14", "id": "CH", "name": "Switzerland", "ext": "", "region": "EU"},
    {
        "_id": "20-0",
        "id": "TT",
        "name": "Trinidad and Tobago",
        "ext": "",
        "region": "US",
    },
    {"_id": "20-1", "id": "TC", "name": "Turks & Caicos", "ext": "", "region": "US"},
    {"_id": "20-2", "id": "TJ", "name": "Tajikistan", "ext": "", "region": "JP"},
    {"_id": "20-3", "id": "TH", "name": "Thailand", "ext": "", "region": "JP"},
    {"_id": "20-4", "id": "TG", "name": "Togo", "ext": "", "region": "JP"},
    {"_id": "20-5", "id": "TR", "name": "Turkey", "ext": "", "region": "EU"},
    {"_id": "20-6", "id": "TM", "name": "Turkmenistan", "ext": "", "region": "JP"},
    {"_id": "21-0", "id": "UA", "name": "Ukraine", "ext": "", "region": "EU"},
    {"_id": "21-1", "id": "GB", "name": "United Kingdom", "ext": "", "region": "EU"},
    {"_id": "21-2", "id": "US", "name": "United States", "ext": "", "region": "US"},
    {"_id": "21-3", "id": "UY", "name": "Uruguay", "ext": "", "region": "US"},
    {
        "_id": "21-4",
        "id": "AE",
        "name": "United Arab Emirates",
        "ext": "",
        "region": "JP",
    },
    {"_id": "21-5", "id": "UZ", "name": "Uzbekistan", "ext": "", "region": "JP"},
    {"_id": "22-0", "id": "VE", "name": "Venezuela", "ext": "", "region": "US"},
    {"_id": "22-1", "id": "VG", "name": "Virgin Islands", "ext": "", "region": "US"},
    {"_id": "22-2", "id": "VN", "name": "Vietnam", "ext": "", "region": "JP"},
    {"_id": "23-0", "id": "YE", "name": "Yemen", "ext": "", "region": "JP"},
]