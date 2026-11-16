project(
  'stopw',
  version: '0.1.0',
  meson_version: '>= 1.0.0',
)

# Outils utilisés pour les traductions, les ressources et les caches GNOME.
i18n = import('i18n')
gnome = import('gnome')

find_program('python3', required: true)

# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------

# Le source reste nommé stopw.py dans le dépôt.
# Le programme installé est nommé stopw, conformément à Exec=stopw.
install_data(
  'stopw.py',
  rename: 'stopw',
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# ---------------------------------------------------------------------------
# Configuration GSettings
# ---------------------------------------------------------------------------

# Installer le XML est indispensable : compiler un schéma absent du
# répertoire d'installation ne suffit pas.
install_data(
  'data/org.eric.stopw.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

# ---------------------------------------------------------------------------
# Intégration au bureau
# ---------------------------------------------------------------------------

i18n.merge_file(
  input: 'data/org.eric.stopw.desktop.in',
  output: 'org.eric.stopw.desktop',
  type: 'desktop',
  po_dir: 'po',
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

i18n.merge_file(
  input: 'data/org.eric.stopw.metainfo.xml.in',
  output: 'org.eric.stopw.metainfo.xml',
  type: 'xml',
  po_dir: 'po',
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

# Le nom demandé est conservé dans les sources : org.eric.stopw.svg.
# Seul le fichier installé prend le nom de l'identifiant d'application,
# afin de correspondre à Icon=org.eric.stopw.
install_data(
  'data/icons/org.eric.stopw.svg',
  rename: 'org.eric.stopw.svg',
  install_dir: (
    get_option('datadir')
    / 'icons'
    / 'hicolor'
    / 'scalable'
    / 'apps'
  ),
)

# ---------------------------------------------------------------------------
# Ressources intégrées
# ---------------------------------------------------------------------------

# Le fichier XML référence complete.wav, situé dans data/.
# Le Python devra explicitement charger le bundle installé.
gnome.compile_resources(
  'org.eric.stopw',
  'data/org.eric.stopw.gresource.xml',
  source_dir: 'data',
  gresource_bundle: true,
  install: true,
  install_dir: get_option('datadir') / 'stopw',
)

# ---------------------------------------------------------------------------
# Traductions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Finalisation de l'installation
# ---------------------------------------------------------------------------

# Meson prend en charge ces opérations ; le script postinstall.py
# fourni précédemment n'est donc pas appelé.
gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
