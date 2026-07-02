""" Main window for the QTGL gui."""
from pathlib import Path
import platform

from PyQt6.QtWidgets import QApplication as app, QWidget, QMainWindow, \
    QSplashScreen, QTreeWidgetItem, QPushButton, QFileDialog, QDialog, \
    QTreeWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QDialogButtonBox, \
    QMenu, QLabel, QDateEdit, QTimeEdit, QSpinBox, QDoubleSpinBox, QComboBox, \
    QGroupBox, QCheckBox
from PyQt6.QtCore import Qt, pyqtSlot, QTimer, QItemSelectionModel, QSize, \
    QEvent, pyqtProperty, QDir, QDate, QTime
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6 import uic

# Local imports
import bluesky as bs
from bluesky.core import Base, Signal
from bluesky.network.discovery import Discovery

from bluesky import stack
from bluesky.stack.argparser import PosArg
from bluesky.pathfinder import ResourcePath
from bluesky.tools.misc import tim2txt
from bluesky.network import subscriber, context as ctx
from bluesky.network.common import get_ownip, seqidx2id, seqid2idx
import bluesky.network.sharedstate as ss

from bluesky.ui import palette

# Child windows
from bluesky.ui.qtgl.docwindow import DocWindow
from bluesky.ui.qtgl.infowindow import InfoWindow
from bluesky.ui.qtgl.settingswindow import SettingsWindow
# from bluesky.ui.qtgl.nd import ND

if platform.system().lower() == "windows":
    from bluesky.ui.pygame.dialog import fileopen
    import winreg
    def isdark():
        ''' Returns true if app is in dark mode, false otherwise. '''
        try:
            registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
            key = winreg.OpenKey(registry, r"Software\\Microsoft\\\Windows\\CurrentVersion\\Themes\\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        except FileNotFoundError:
            return False
else:
    def isdark():
        ''' Returns true if app is in dark mode, false otherwise. '''
        p = app.instance().style().standardPalette()
        return (p.color(p.ColorRole.Window).value() < p.color(p.ColorRole.WindowText).value())


# Register settings defaults
bs.settings.set_variable_defaults(gfx_path='graphics', start_location='EHAM')

palette.set_default_colours(stack_text=(0, 255, 0),
                            stack_background=(102, 102, 102))


class Splash(QSplashScreen):
    """ Splash screen: BlueSky logo during start-up"""
    def __init__(self):
        splashfile = bs.resource(bs.settings.gfx_path) / 'splash.gif'
        super().__init__(QPixmap(splashfile.as_posix()), Qt.WindowType.WindowStaysOnTopHint)


class DiscoveryDialog(QDialog):
    def __init__(self, comm_id, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumSize(200,200) # To prevent Geometry error
        self.servers = []
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.serverview = QTreeWidget()
        self.serverview.setHeaderLabels(['Server', 'Ports'])
        self.serverview.setIndentation(0)
        self.serverview.setStyleSheet('padding:0px')
        self.serverview.header().resizeSection(0, 180)
        layout.addWidget(self.serverview)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(self.on_accept)
        btns.rejected.connect(parent.closeEvent)

        self.discovery = Discovery(comm_id)

        self.discovery_timer = QTimer()
        self.discovery_timer.timeout.connect(self.discovery.update)
        self.discovery_timer.start(1000)
        self.discovery.server_discovered.connect(self.add_srv)
        self.discovery.start()

    def add_srv(self, address, ports):
        for server in self.servers:
            if address == server.address and ports == server.ports:
                # We already know this server, skip
                return
        server = QTreeWidgetItem(self.serverview)
        server.address = address
        server.ports = ports
        server.hostname = 'This computer' if address == get_ownip() else address
        server.setText(0, server.hostname)

        server.setText(1, '{},{}'.format(*ports))
        self.servers.append(server)

    def on_accept(self):
        server = self.serverview.currentItem()
        if server:
            self.discovery_timer.stop()
            self.discovery.stop()
            hostname = server.address
            rport, sport = server.ports
            bs.net.connect(hostname=hostname, recv_port=rport, send_port=sport)
            self.close()


_ESSA_BBOX = dict(lamin=57.7512, lomin=14.6317, lamax=61.1313, lomax=21.4669)
_PRESETS = {
    'ESSA Arlanda TMA': _ESSA_BBOX,
}


class OpenSkyDialog(QDialog):
    """Dialog for fetching and replaying historical OpenSky traffic."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Load OpenSky Historical Traffic')
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMinimumHeight(480)

        root = QVBoxLayout(self)

        # --- Credential warning ---
        self._warn_label = QLabel()
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet(
            'background:#7a6000;color:#ffe066;padding:6px;border-radius:4px;'
        )
        self._warn_label.setVisible(False)
        root.addWidget(self._warn_label)

        # --- Date/time/duration ---
        dt_group = QGroupBox('Time (UTC)')
        dt_form = QFormLayout(dt_group)

        # Default to today at (now - 30 min) so the window is within the live 60-min limit
        from PyQt6.QtCore import QDateTime
        now_qt = QDateTime.currentDateTimeUtc()
        default_dt = now_qt.addSecs(-1800)
        self.date_edit = QDateEdit(default_dt.date())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        dt_form.addRow('Date:', self.date_edit)

        self.time_edit = QTimeEdit(default_dt.time())
        self.time_edit.setDisplayFormat('HH:mm')
        dt_form.addRow('End Time (UTC):', self.time_edit)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 480)
        self.duration_spin.setValue(30)
        self.duration_spin.setSuffix(' min')
        dt_form.addRow('Duration:', self.duration_spin)

        root.addWidget(dt_group)

        # --- Area ---
        area_group = QGroupBox('Area')
        area_layout = QVBoxLayout(area_group)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel('Preset:'))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(_PRESETS.keys()) + ['Custom'])
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self.preset_combo)
        area_layout.addLayout(preset_row)

        bbox_form = QFormLayout()

        self.lamin_spin = QDoubleSpinBox()
        self.lamin_spin.setRange(-90, 90)
        self.lamin_spin.setDecimals(4)
        self.lamin_spin.setSingleStep(0.5)
        bbox_form.addRow('Lat min:', self.lamin_spin)

        self.lomin_spin = QDoubleSpinBox()
        self.lomin_spin.setRange(-180, 180)
        self.lomin_spin.setDecimals(4)
        self.lomin_spin.setSingleStep(0.5)
        bbox_form.addRow('Lon min:', self.lomin_spin)

        self.lamax_spin = QDoubleSpinBox()
        self.lamax_spin.setRange(-90, 90)
        self.lamax_spin.setDecimals(4)
        self.lamax_spin.setSingleStep(0.5)
        bbox_form.addRow('Lat max:', self.lamax_spin)

        self.lomax_spin = QDoubleSpinBox()
        self.lomax_spin.setRange(-180, 180)
        self.lomax_spin.setDecimals(4)
        self.lomax_spin.setSingleStep(0.5)
        bbox_form.addRow('Lon max:', self.lomax_spin)

        area_layout.addLayout(bbox_form)
        root.addWidget(area_group)

        # --- Buttons ---
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        # Apply first preset
        self._on_preset_changed(self.preset_combo.currentText())
        self._check_credentials()

    # ------------------------------------------------------------------

    def _check_credentials(self):
        client_id = getattr(bs.settings, 'opensky_client_id', '')
        if not client_id:
            self._warn_label.setText(
                '\u26a0  OpenSky credentials not configured.\n'
                'Add opensky_client_id and opensky_client_secret to settings.cfg.'
            )
            self._warn_label.setVisible(True)
        else:
            self._warn_label.setVisible(False)

    def _on_preset_changed(self, name: str):
        bbox = _PRESETS.get(name)
        is_custom = bbox is None
        for spin in (self.lamin_spin, self.lomin_spin, self.lamax_spin, self.lomax_spin):
            spin.setReadOnly(not is_custom)
        if bbox:
            self.lamin_spin.setValue(bbox['lamin'])
            self.lomin_spin.setValue(bbox['lomin'])
            self.lamax_spin.setValue(bbox['lamax'])
            self.lomax_spin.setValue(bbox['lomax'])

    def _on_accept(self):
        date_str = self.date_edit.date().toString('yyyy-MM-dd')
        time_str = self.time_edit.time().toString('HH:mm')
        dt_str = f'{date_str}T{time_str}'

        lamin = self.lamin_spin.value()
        lomin = self.lomin_spin.value()
        lamax = self.lamax_spin.value()
        lomax = self.lomax_spin.value()
        duration = self.duration_spin.value()

        cmd = f'LOADOPENSKY {dt_str} {lamin} {lomin} {lamax} {lomax} {duration}'
        stack.stack(cmd)
        self.accept()


class TMAOptDialog(QDialog):
    """Dialog for running TMA optimization on historical OpenSky traffic."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('TMA Optimization')
        self.setModal(True)
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Data source toggle ────────────────────────────────────────
        from PyQt6.QtWidgets import QRadioButton, QButtonGroup, QLineEdit
        src_group  = QGroupBox('Data Source')
        src_layout = QHBoxLayout(src_group)
        self.rb_trino = QRadioButton('OpenSky Trino')
        self.rb_file  = QRadioButton('Local CSV file')
        self.rb_trino.setChecked(True)
        src_layout.addWidget(self.rb_trino)
        src_layout.addWidget(self.rb_file)
        root.addWidget(src_group)

        # File picker row (hidden until rb_file selected)
        self.file_widget = QWidget()
        file_layout = QHBoxLayout(self.file_widget)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText('Path to _historical.csv or _tracks.csv …')
        self.file_edit.setReadOnly(True)
        file_btn = QPushButton('Browse…')
        file_btn.clicked.connect(self._browse_csv)
        file_layout.addWidget(self.file_edit)
        file_layout.addWidget(file_btn)
        root.addWidget(self.file_widget)
        self.file_widget.setVisible(False)

        self.rb_trino.toggled.connect(self._on_source_toggled)
        self.rb_file.toggled.connect(self._on_source_toggled)

        # ── Time window ──────────────────────────────────────────────
        dt_group = QGroupBox('Traffic Time Window (UTC)')
        dt_form  = QFormLayout(dt_group)

        from PyQt6.QtCore import QDateTime
        now_qt     = QDateTime.currentDateTimeUtc()
        default_dt = now_qt.addSecs(-1800)

        self.date_edit = QDateEdit(default_dt.date())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        dt_form.addRow('Date:', self.date_edit)

        self.time_edit = QTimeEdit(default_dt.time())
        self.time_edit.setDisplayFormat('HH:mm')
        dt_form.addRow('End Time (UTC):', self.time_edit)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 480)
        self.duration_spin.setValue(60)
        self.duration_spin.setSuffix(' min')
        dt_form.addRow('Duration:', self.duration_spin)

        self.dt_group = dt_group
        root.addWidget(dt_group)

        # ── Entry points ─────────────────────────────────────────────
        entry_group = QGroupBox('Active Entry Points')
        entry_layout = QHBoxLayout(entry_group)
        self.cb_N = QCheckBox('N'); self.cb_N.setChecked(True)
        self.cb_E = QCheckBox('E'); self.cb_E.setChecked(True)
        self.cb_S = QCheckBox('S'); self.cb_S.setChecked(True)
        self.cb_W = QCheckBox('W'); self.cb_W.setChecked(True)
        for cb in (self.cb_N, self.cb_E, self.cb_S, self.cb_W):
            entry_layout.addWidget(cb)
        root.addWidget(entry_group)

        # ── Optimisation parameters ───────────────────────────────────
        opt_group = QGroupBox('Optimisation Parameters')
        opt_form  = QFormLayout(opt_group)

        self.max_ac_spin = QSpinBox()
        self.max_ac_spin.setRange(2, 40)
        self.max_ac_spin.setValue(15)
        self.max_ac_spin.setToolTip('Total aircraft sent to Gurobi (spread evenly across active entries)')
        opt_form.addRow('Max aircraft:', self.max_ac_spin)

        self.max_ac_per_entry_spin = QSpinBox()
        self.max_ac_per_entry_spin.setRange(1, 10)
        self.max_ac_per_entry_spin.setValue(5)
        self.max_ac_per_entry_spin.setToolTip('Maximum aircraft per entry node')
        opt_form.addRow('Max ac / entry:', self.max_ac_per_entry_spin)

        self.max_eps_spin = QSpinBox()
        self.max_eps_spin.setRange(0, 10)
        self.max_eps_spin.setValue(3)
        self.max_eps_spin.setSuffix(' min')
        self.max_eps_spin.setToolTip('Maximum epsilon (±min flexibility around estimated entry time). Sequence: 0 → 2 → 3 → … → max')
        opt_form.addRow('Max epsilon:', self.max_eps_spin)

        self.time_limit_spin = QSpinBox()
        self.time_limit_spin.setRange(30, 600)
        self.time_limit_spin.setValue(120)
        self.time_limit_spin.setSuffix(' s')
        self.time_limit_spin.setToolTip('Gurobi time limit per epsilon attempt')
        opt_form.addRow('Time limit / attempt:', self.time_limit_spin)

        self.s1_spin = QSpinBox()
        self.s1_spin.setRange(1, 10)
        self.s1_spin.setValue(2)
        self.s1_spin.setSuffix(' min')
        self.s1_spin.setToolTip('Minimum separation Heavy–Medium at merge/runway')
        opt_form.addRow('Sep Heavy–Medium (s1):', self.s1_spin)

        self.s2_spin = QSpinBox()
        self.s2_spin.setRange(1, 10)
        self.s2_spin.setValue(3)
        self.s2_spin.setSuffix(' min')
        self.s2_spin.setToolTip('Minimum separation Medium–Light or Light–Medium')
        opt_form.addRow('Sep Medium–Light (s2):', self.s2_spin)

        self.fetch_radius_spin = QSpinBox()
        self.fetch_radius_spin.setRange(10, 400)
        self.fetch_radius_spin.setValue(50)
        self.fetch_radius_spin.setSuffix(' nm')
        self.fetch_radius_spin.setToolTip('Radius around ESSA for Trino data fetch')
        opt_form.addRow('Fetch radius:', self.fetch_radius_spin)
        self._fetch_radius_label = opt_form.labelForField(self.fetch_radius_spin)

        self.opt_group = opt_group
        root.addWidget(opt_group)

        # ── CDO Parameters ────────────────────────────────────────────
        cdo_group = QGroupBox('CDO Parameters')
        cdo_form  = QFormLayout(cdo_group)

        self.cdo_fap_alt_spin = QSpinBox()
        self.cdo_fap_alt_spin.setRange(500, 5000)
        self.cdo_fap_alt_spin.setValue(2500)
        self.cdo_fap_alt_spin.setSuffix(' ft')
        self.cdo_fap_alt_spin.setToolTip('Final Approach Point altitude — CDO ends here')
        cdo_form.addRow('FAP altitude:', self.cdo_fap_alt_spin)

        self.cdo_ias_start_spin = QSpinBox()
        self.cdo_ias_start_spin.setRange(100, 350)
        self.cdo_ias_start_spin.setValue(200)
        self.cdo_ias_start_spin.setSuffix(' kt')
        self.cdo_ias_start_spin.setToolTip('CAS at FAP (CDO start speed)')
        cdo_form.addRow('IAS at FAP:', self.cdo_ias_start_spin)

        self.cdo_ias_restrict_spin = QSpinBox()
        self.cdo_ias_restrict_spin.setRange(150, 350)
        self.cdo_ias_restrict_spin.setValue(220)
        self.cdo_ias_restrict_spin.setSuffix(' kt')
        self.cdo_ias_restrict_spin.setToolTip('Max CAS on approach arcs (IAS restriction)')
        cdo_form.addRow('IAS restriction:', self.cdo_ias_restrict_spin)

        self.cdo_mach_spin = QDoubleSpinBox()
        self.cdo_mach_spin.setRange(0.60, 0.95)
        self.cdo_mach_spin.setValue(0.84)
        self.cdo_mach_spin.setSingleStep(0.01)
        self.cdo_mach_spin.setDecimals(2)
        self.cdo_mach_spin.setToolTip('Mach number in upper descent (above Mach/CAS crossover)')
        cdo_form.addRow('M descent:', self.cdo_mach_spin)

        self.cdo_mlw_spin = QDoubleSpinBox()
        self.cdo_mlw_spin.setRange(0.5, 1.0)
        self.cdo_mlw_spin.setValue(0.9)
        self.cdo_mlw_spin.setSingleStep(0.05)
        self.cdo_mlw_spin.setDecimals(2)
        self.cdo_mlw_spin.setToolTip('Aircraft mass as fraction of MLW (e.g. 0.9 = 90% MLW)')
        cdo_form.addRow('Mass (×MLW):', self.cdo_mlw_spin)

        self.cdo_kt_per_sec_spin = QDoubleSpinBox()
        self.cdo_kt_per_sec_spin.setRange(0.1, 5.0)
        self.cdo_kt_per_sec_spin.setValue(1.0)
        self.cdo_kt_per_sec_spin.setSingleStep(0.1)
        self.cdo_kt_per_sec_spin.setDecimals(1)
        self.cdo_kt_per_sec_spin.setToolTip('Speed deceleration rate between altitude bands (kt/s)')
        cdo_form.addRow('Decel rate:', self.cdo_kt_per_sec_spin)

        self.cdo_wind_cb = QCheckBox('Use ERA5 wind/temperature')
        self.cdo_wind_cb.setChecked(True)
        self.cdo_wind_cb.setToolTip('Use ERA5 reanalysis wind and temperature in CDO physics')
        cdo_form.addRow('', self.cdo_wind_cb)

        self.cdo_c_v_min_spin = QDoubleSpinBox()
        self.cdo_c_v_min_spin.setRange(1.0, 1.5)
        self.cdo_c_v_min_spin.setValue(1.23)
        self.cdo_c_v_min_spin.setSingleStep(0.01)
        self.cdo_c_v_min_spin.setDecimals(2)
        self.cdo_c_v_min_spin.setToolTip('BADA minimum speed coefficient (fraction of stall speed)')
        cdo_form.addRow('C_v_min:', self.cdo_c_v_min_spin)

        root.addWidget(cdo_group)

        # ── Buttons ───────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _on_source_toggled(self):
        use_file = self.rb_file.isChecked()
        self.file_widget.setVisible(use_file)
        self.dt_group.setVisible(not use_file)
        self.fetch_radius_spin.setVisible(not use_file)
        if self._fetch_radius_label:
            self._fetch_radius_label.setVisible(not use_file)

    def _browse_csv(self):
        import platform
        from pathlib import Path
        start = str(Path(__file__).resolve().parents[3] / 'scenario')
        if platform.system().lower() == 'darwin':
            response = QFileDialog.getOpenFileName(
                self, 'Select historical CSV', start,
                'CSV files (*.csv)')
        else:
            response = QFileDialog.getOpenFileName(
                self, 'Select historical CSV', start,
                'CSV files (*.csv)',
                options=QFileDialog.Option.DontUseNativeDialog)
        fname = response[0] if isinstance(response, tuple) else response
        if fname:
            self.file_edit.setText(fname)

    def _on_accept(self):
        entries = ''.join(
            d for d, cb in (('N', self.cb_N), ('E', self.cb_E),
                            ('S', self.cb_S), ('W', self.cb_W)) if cb.isChecked()
        ) or 'NESW'
        max_ac           = self.max_ac_spin.value()
        max_ac_per_entry = self.max_ac_per_entry_spin.value()
        max_eps          = self.max_eps_spin.value()
        time_limit       = self.time_limit_spin.value()
        s1               = self.s1_spin.value()
        s2               = self.s2_spin.value()
        cdo_fap_alt      = self.cdo_fap_alt_spin.value()
        cdo_ias_start    = self.cdo_ias_start_spin.value()
        cdo_ias_restrict = self.cdo_ias_restrict_spin.value()
        cdo_mach         = self.cdo_mach_spin.value()
        cdo_mlw          = self.cdo_mlw_spin.value()
        cdo_kt_per_sec   = self.cdo_kt_per_sec_spin.value()
        cdo_wind         = int(self.cdo_wind_cb.isChecked())
        cdo_c_v_min      = self.cdo_c_v_min_spin.value()
        cdo_args = (f'{cdo_fap_alt} {cdo_ias_start} {cdo_ias_restrict} {cdo_mach} '
                    f'{cdo_mlw} {cdo_kt_per_sec} {cdo_wind} {cdo_c_v_min}')

        if self.rb_file.isChecked():
            fname = self.file_edit.text().strip()
            if not fname:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'No file selected', 'Please select a CSV file first.')
                return
            stack.stack(
                f'TMAOPTFILE "{fname}" {entries} {max_ac} {max_ac_per_entry} '
                f'{max_eps} {time_limit} {s1} {s2} {cdo_args}'
            )
        else:
            date_str     = self.date_edit.date().toString('yyyy-MM-dd')
            time_str     = self.time_edit.time().toString('HH:mm')
            dt_str       = f'{date_str}T{time_str}'
            duration     = self.duration_spin.value()
            fetch_radius = self.fetch_radius_spin.value()
            stack.stack(
                f'TMAOPT {dt_str} {duration} {entries} {max_ac} {max_ac_per_entry} '
                f'{max_eps} {time_limit} {s1} {s2} {fetch_radius} {cdo_args}'
            )
        self.accept()


class TMARollingDialog(QDialog):
    """Dialog for running the rolling TMA optimizer over a ≥2h window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Rolling TMA Optimization')
        self.setModal(True)
        self.setMinimumWidth(400)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Time window ──────────────────────────────────────────────
        dt_group = QGroupBox('Traffic Time Window (UTC)')
        dt_form  = QFormLayout(dt_group)

        from PyQt6.QtCore import QDateTime
        now_qt     = QDateTime.currentDateTimeUtc()
        default_dt = now_qt.addSecs(-3600)

        self.date_edit = QDateEdit(default_dt.date())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        dt_form.addRow('Date:', self.date_edit)

        self.time_edit = QTimeEdit(default_dt.time())
        self.time_edit.setDisplayFormat('HH:mm')
        dt_form.addRow('End Time (UTC):', self.time_edit)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(120, 480)
        self.duration_spin.setValue(120)
        self.duration_spin.setSuffix(' min')
        self.duration_spin.setToolTip('Total rolling window (minimum 120 min = 2 hours)')
        dt_form.addRow('Total Duration:', self.duration_spin)

        self.dt_group = dt_group
        root.addWidget(dt_group)

        # ── Entry points ─────────────────────────────────────────────
        entry_group  = QGroupBox('Active Entry Points')
        entry_layout = QHBoxLayout(entry_group)
        self.cb_N = QCheckBox('N'); self.cb_N.setChecked(True)
        self.cb_E = QCheckBox('E'); self.cb_E.setChecked(True)
        self.cb_S = QCheckBox('S'); self.cb_S.setChecked(True)
        self.cb_W = QCheckBox('W'); self.cb_W.setChecked(True)
        for cb in (self.cb_N, self.cb_E, self.cb_S, self.cb_W):
            entry_layout.addWidget(cb)
        root.addWidget(entry_group)

        # ── Optimisation parameters ───────────────────────────────────
        opt_group = QGroupBox('Optimisation Parameters')
        opt_form  = QFormLayout(opt_group)

        self.max_ac_spin = QSpinBox()
        self.max_ac_spin.setRange(2, 40)
        self.max_ac_spin.setValue(15)
        opt_form.addRow('Max aircraft:', self.max_ac_spin)

        self.max_ac_per_entry_spin = QSpinBox()
        self.max_ac_per_entry_spin.setRange(1, 10)
        self.max_ac_per_entry_spin.setValue(5)
        opt_form.addRow('Max ac / entry:', self.max_ac_per_entry_spin)

        self.max_eps_spin = QSpinBox()
        self.max_eps_spin.setRange(0, 10)
        self.max_eps_spin.setValue(3)
        self.max_eps_spin.setSuffix(' min')
        opt_form.addRow('Max epsilon:', self.max_eps_spin)

        self.time_limit_spin = QSpinBox()
        self.time_limit_spin.setRange(30, 600)
        self.time_limit_spin.setValue(120)
        self.time_limit_spin.setSuffix(' s')
        opt_form.addRow('Time limit / attempt:', self.time_limit_spin)

        self.s1_spin = QSpinBox()
        self.s1_spin.setRange(1, 10)
        self.s1_spin.setValue(2)
        self.s1_spin.setSuffix(' min')
        opt_form.addRow('Sep Heavy–Medium (s1):', self.s1_spin)

        self.s2_spin = QSpinBox()
        self.s2_spin.setRange(1, 10)
        self.s2_spin.setValue(3)
        self.s2_spin.setSuffix(' min')
        opt_form.addRow('Sep Medium–Light (s2):', self.s2_spin)

        self.fetch_radius_spin = QSpinBox()
        self.fetch_radius_spin.setRange(10, 400)
        self.fetch_radius_spin.setValue(50)
        self.fetch_radius_spin.setSuffix(' nm')
        opt_form.addRow('Fetch radius:', self.fetch_radius_spin)

        root.addWidget(opt_group)

        # ── CDO Parameters ────────────────────────────────────────────
        cdo_group = QGroupBox('CDO Parameters')
        cdo_form  = QFormLayout(cdo_group)

        self.cdo_fap_alt_spin = QSpinBox()
        self.cdo_fap_alt_spin.setRange(500, 5000)
        self.cdo_fap_alt_spin.setValue(2500)
        self.cdo_fap_alt_spin.setSuffix(' ft')
        cdo_form.addRow('FAP altitude:', self.cdo_fap_alt_spin)

        self.cdo_ias_start_spin = QSpinBox()
        self.cdo_ias_start_spin.setRange(100, 350)
        self.cdo_ias_start_spin.setValue(200)
        self.cdo_ias_start_spin.setSuffix(' kt')
        cdo_form.addRow('IAS at FAP:', self.cdo_ias_start_spin)

        self.cdo_ias_restrict_spin = QSpinBox()
        self.cdo_ias_restrict_spin.setRange(150, 350)
        self.cdo_ias_restrict_spin.setValue(220)
        self.cdo_ias_restrict_spin.setSuffix(' kt')
        cdo_form.addRow('IAS restriction:', self.cdo_ias_restrict_spin)

        self.cdo_mach_spin = QDoubleSpinBox()
        self.cdo_mach_spin.setRange(0.60, 0.95)
        self.cdo_mach_spin.setValue(0.84)
        self.cdo_mach_spin.setSingleStep(0.01)
        self.cdo_mach_spin.setDecimals(2)
        cdo_form.addRow('M descent:', self.cdo_mach_spin)

        self.cdo_mlw_spin = QDoubleSpinBox()
        self.cdo_mlw_spin.setRange(0.5, 1.0)
        self.cdo_mlw_spin.setValue(0.9)
        self.cdo_mlw_spin.setSingleStep(0.05)
        self.cdo_mlw_spin.setDecimals(2)
        cdo_form.addRow('Mass (×MLW):', self.cdo_mlw_spin)

        self.cdo_kt_per_sec_spin = QDoubleSpinBox()
        self.cdo_kt_per_sec_spin.setRange(0.1, 5.0)
        self.cdo_kt_per_sec_spin.setValue(1.0)
        self.cdo_kt_per_sec_spin.setSingleStep(0.1)
        self.cdo_kt_per_sec_spin.setDecimals(1)
        cdo_form.addRow('Decel rate:', self.cdo_kt_per_sec_spin)

        self.cdo_wind_cb = QCheckBox('Use ERA5 wind/temperature')
        self.cdo_wind_cb.setChecked(True)
        cdo_form.addRow('', self.cdo_wind_cb)

        self.cdo_c_v_min_spin = QDoubleSpinBox()
        self.cdo_c_v_min_spin.setRange(1.0, 1.5)
        self.cdo_c_v_min_spin.setValue(1.23)
        self.cdo_c_v_min_spin.setSingleStep(0.01)
        self.cdo_c_v_min_spin.setDecimals(2)
        cdo_form.addRow('C_v_min:', self.cdo_c_v_min_spin)

        root.addWidget(cdo_group)

        # ── Buttons ───────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _on_accept(self):
        date_str = self.date_edit.date().toString('yyyy-MM-dd')
        time_str = self.time_edit.time().toString('HH:mm')
        dt_str   = f'{date_str}T{time_str}'
        duration = self.duration_spin.value()
        entries  = ''.join(
            d for d, cb in (('N', self.cb_N), ('E', self.cb_E),
                            ('S', self.cb_S), ('W', self.cb_W)) if cb.isChecked()
        ) or 'NESW'
        max_ac           = self.max_ac_spin.value()
        max_ac_per_entry = self.max_ac_per_entry_spin.value()
        max_eps          = self.max_eps_spin.value()
        time_limit       = self.time_limit_spin.value()
        s1               = self.s1_spin.value()
        s2               = self.s2_spin.value()
        fetch_radius     = self.fetch_radius_spin.value()
        cdo_fap_alt      = self.cdo_fap_alt_spin.value()
        cdo_ias_start    = self.cdo_ias_start_spin.value()
        cdo_ias_restrict = self.cdo_ias_restrict_spin.value()
        cdo_mach         = self.cdo_mach_spin.value()
        cdo_mlw          = self.cdo_mlw_spin.value()
        cdo_kt_per_sec   = self.cdo_kt_per_sec_spin.value()
        cdo_wind         = int(self.cdo_wind_cb.isChecked())
        cdo_c_v_min      = self.cdo_c_v_min_spin.value()
        stack.stack(
            f'TMAROLLING {dt_str} {duration} {entries} {max_ac} {max_ac_per_entry} '
            f'{max_eps} {time_limit} {s1} {s2} {fetch_radius} '
            f'{cdo_fap_alt} {cdo_ias_start} {cdo_ias_restrict} {cdo_mach} '
            f'{cdo_mlw} {cdo_kt_per_sec} {cdo_wind} {cdo_c_v_min}'
        )
        self.accept()


class MainWindow(QMainWindow, Base):
    """ Qt window process: from .ui file read UI window-definition of main window """

    modes = ['Init', 'Hold', 'Operate', 'End']

    # Per remote node attributes
    nconf_cur: ss.ActData[int] = ss.ActData(0, group='acdata')
    nconf_tot: ss.ActData[int] = ss.ActData(0, group='acdata')
    nlos_cur: ss.ActData[int] = ss.ActData(0, group='acdata')
    nlos_tot: ss.ActData[int] = ss.ActData(0, group='acdata')

    show_map: ss.ActData[bool] = ss.ActData(True)
    show_coast: ss.ActData[bool] = ss.ActData(True)
    show_wpt: ss.ActData[int] = ss.ActData(1)
    show_apt: ss.ActData[int] = ss.ActData(1)
    show_pz: ss.ActData[bool] = ss.ActData(False)
    show_traf: ss.ActData[bool] = ss.ActData(True)
    show_lbl: ss.ActData[int] = ss.ActData(2)

    @pyqtProperty(str)
    def style(self):
        ''' Returns "dark"" if app is in dark mode, "light" otherwise. '''
        return "dark" if self.darkmode else "light"

    def __init__(self, mode):
        super().__init__()
        # Running mode of this gui. Options:
        #  - server-gui: Normal mode, starts bluesky server together with gui
        #  - client: starts only gui in client mode, can connect to existing
        #    server.
        self.mode = mode
        self.running = True

        # self.nd = ND(shareWidget=self.radarwidget)
        self.infowin = InfoWindow()
        self.settingswin = SettingsWindow()
        self.darkmode = isdark()

        try:
            self.docwin = DocWindow(self)
        except Exception as e:
            print('Couldnt make docwindow:', e)
        # self.aman = AMANDisplay()
        

        gfxpath = bs.resource(bs.settings.gfx_path)

        if platform.system() == 'Darwin':
            app.instance().setWindowIcon(QIcon((gfxpath / 'bluesky.icns').as_posix()))
        else:
            app.instance().setWindowIcon(QIcon((gfxpath / 'icon.gif').as_posix()))

        uic.loadUi((gfxpath / 'mainwindow.ui').as_posix(), self)
        gltimer = QTimer(self)
        gltimer.timeout.connect(self.radarwidget.update)
        # gltimer.timeout.connect(self.nd.updateGL)
        gltimer.start(50)

        # If multiple scenario paths exist, add 'Open From' menu
        scenresource = bs.resource('scenario')
        if isinstance(scenresource, ResourcePath) and scenresource.nbases > 1:
            openfrom = QMenu('Open From', self.menuFile)
            self.menuFile.insertMenu(self.action_Save, openfrom)

            openpkg = openfrom.addAction('Package')
            openpkg.triggered.connect(lambda: self.show_file_dialog(scenresource.base(-1)))
            openusr = openfrom.addAction('User')
            openusr.triggered.connect(lambda: self.show_file_dialog(scenresource.base(0)))

        # Link menubar buttons
        self.action_Open.triggered.connect(self.show_file_dialog)
        self.action_Save.triggered.connect(self.buttonClicked)
        self.actionBlueSky_help.triggered.connect(self.show_doc_window)
        self.actionSettings.triggered.connect(self.settingswin.show)

        # OSHist button wired via buttonClicked() below (custom1 in Cust block).

        # Connect to io client's nodelist changed signal
        bs.net.node_added.connect(self.nodesChanged)
        bs.net.server_added.connect(self.serversChanged)

        # Tell BlueSky that this is the screen object for this client
        bs.scr = self

        # Signals we want to emit
        self.panzoom_event = Signal('state-changed.panzoom')

        # Set position default from settings
        lat, lon, _ = PosArg().parse(bs.settings.start_location)
        ss.setdefault('pan', [lat, lon], group='panzoom')


        # self.nodetree.setVisible(False)
        self.nodetree.setIndentation(0)
        self.nodetree.setColumnCount(2)
        self.nodetree.setStyleSheet('padding:0px')
        self.nodetree.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.nodetree.header().resizeSection(0, 130)
        self.nodetree.itemClicked.connect(self.nodetreeClicked)
        self.maxservnum = 0
        self.servers = dict()
        self.nodes = dict()
        self.actnode = ''

        self.splitter.setSizes([1, 0])
        self.splitter_2.setSizes([1, 0])
        self.setStyleSheet()

        # Remove keyboard focus from all children of self.databox
        # (not possible to do in QDesigner)
        def recursiveNoFocus(w):
            for child in w.findChildren(QWidget):
                recursiveNoFocus(child)
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        recursiveNoFocus(self.databox)

    def setStyleSheet(self, contents=''):
        if not contents:
            QDir.addSearchPath("icons", (bs.resource(bs.settings.gfx_path) / "icons").as_posix())
            with open(bs.resource(bs.settings.gfx_path) / 'bluesky.qss') as style:
                super().setStyleSheet(style.read())

    @stack.command
    def swrad(self, switch: 'txt', arg: int|None = None):
        ''' Switch on/off elements and background of map/radar view 
        
            Usage:
                SWRAD SAT/GEO/GRID/APT/VOR/WPT/LABEL/ADSBCOVERAGE/TRAIL/POLY [dt]/[value]
        '''
        match switch:
            case 'GEO':
                self.show_coast = not self.show_coast
            case 'SAT':
                self.show_map = not self.show_map
            case 'APT':
                if arg is not None:
                    self.show_apt = min(2,max(0,arg))
                else:
                    self.show_apt = (self.show_apt + 1) % 3
            case 'WPT':
                if arg is not None:
                    self.show_wpt = min(2,max(0,arg))
                else:
                    self.show_wpt = (self.show_wpt + 1) % 3
            case 'LABEL':
                if arg is not None:
                    self.show_lbl = min(2,max(0,arg))
                else:
                    self.show_lbl = (self.show_lbl + 1) % 3
            case 'SYM':
                if arg is None:
                    arg = 0 if self.show_pz else (2 if self.show_traf else 1)
                self.show_traf = arg > 0
                self.show_pz = arg > 1

    @stack.command
    def mcre(self, args: 'string'):
        """ Create one or more random aircraft in a specified area 
        
            When called from the client (gui), MCRE will use the current screen bounds as area to create aircraft in.
            When called from a scenario, the simulation reference area will be used.
        """
        if not args:
            return stack.forward('MCRE')

        stack.forward(f'INSIDE {" ".join(str(el) for el in bs.ref.area.bbox)} MCRE {args}')

    @stack.command(annotations='pandir/latlon', brief='PAN latlon/acid/airport/waypoint/LEFT/RIGHT/UP/DOWN')
    def pan(self, *args):
        "Pan screen (move view) to a waypoint, direction or aircraft"
        store = ss.get(group='panzoom')
        store.pan = list(args)
        self.panzoom_event.emit(store)
        return True

    @stack.commandgroup(brief='ZOOM IN/OUT/factor')
    def zoom(self, factor: float):
        ''' ZOOM: Zoom in and out in the radar view. 
        
            Arguments:
            - factor: IN/OUT to zoom in/out by a factor sqrt(2), or
                      'factor' to set zoom to specific value.
        '''
        store = ss.get(group='panzoom')
        store.zoom = factor
        self.panzoom_event.emit(store)
        return True

    @zoom.subcommand(name='IN')
    def zoomin(self, factor:float|None=None):
        ''' ZOOM IN: change zoom level up, relative to previous value '''
        store = ss.get(group='panzoom')
        store.zoom *= (factor or 1.4142135623730951)
        self.panzoom_event.emit(store)
        return True

    @zoom.subcommand(name='OUT')
    def zoomout(self, factor:float|None=None):
        ''' ZOOM OUT: change zoom level down, relative to previous value '''
        store = ss.get(group='panzoom')
        store.zoom *= (factor or 0.7071067811865475)
        self.panzoom_event.emit(store)
        return True

    def getviewctr(self):
        return self.radarwidget.pan

    def getviewbounds(self): # Return current viewing area in lat, lon
        return self.radarwidget.viewportlatlon()

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier \
                and event.key() in [Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right]:
            dlat = 1.0 / (self.radarwidget.zoom * self.radarwidget.ar)
            dlon = 1.0 / (self.radarwidget.zoom * self.radarwidget.flat_earth)
            if event.key() == Qt.Key.Key_Up:
                self.radarwidget.setpanzoom(pan=(dlat, 0.0), absolute=False)
            elif event.key() == Qt.Key.Key_Down:
                self.radarwidget.setpanzoom(pan=(-dlat, 0.0), absolute=False)
            elif event.key() == Qt.Key.Key_Left:
                self.radarwidget.setpanzoom(pan=(0.0, -dlon), absolute=False)
            elif event.key() == Qt.Key.Key_Right:
                self.radarwidget.setpanzoom(pan=(0.0, dlon), absolute=False)

        elif event.key() == Qt.Key.Key_Escape:
            self.closeEvent()

        elif event.key() == Qt.Key.Key_F11:  # F11 = Toggle Full Screen mode
            if not self.isFullScreen():
                self.showFullScreen()
            else:
                self.showNormal()

        else:
            # All other events go to the BlueSky console
            self.console.keyPressEvent(event)
        event.accept()

    @stack.command(name='QUIT', annotations='', aliases=('CLOSE', 'END', 'EXIT', 'Q', 'STOP'))
    def closeEvent(self, event=None):
        if self.running:
            self.running = False
            # Send quit to server if we own it
            if self.mode != 'client':
                bs.net.send(b'QUIT', to_group=bs.server.server_id)
            app.instance().closeAllWindows()
            # return True

    @subscriber
    def echo(self, text, flags=None, sender_id=None):
        refnode = sender_id or ctx.sender_id or bs.net.act_id
        # Always update the store
        store = ss.get(refnode)
        store.echotext.append(text)
        store.echoflags.append(flags)
        # Directly echo if message corresponds to active node
        if refnode == bs.net.act_id:
            return self.console.echo(text, flags)

    def changeEvent(self, event: QEvent):
        # Detect dark/light mode switch
        if event.type() == event.Type.PaletteChange and self.darkmode != isdark():
            self.darkmode = isdark()
            self.setStyleSheet()

        return super().changeEvent(event)


    def actnodedataChanged(self, nodeid, nodedata, changed_elems):
        if nodeid != self.actnode:
            self.actnode = nodeid
            node = self.nodes[nodeid]
            self.nodelabel.setText(f'<b>Node</b> {node.serv_num}:{node.node_num}')
            self.nodetree.setCurrentItem(node, 0, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def serversChanged(self, server_id):
        server = self.servers.get(server_id)
        if not server:
            server = QTreeWidgetItem(self.nodetree)
            self.maxservnum += 1
            server.serv_num = self.maxservnum
            server.server_id = server_id
            hostname = 'Ungrouped' if server_id == b'0' else 'This computer'
            f = server.font(0)
            f.setBold(True)
            server.setExpanded(True)
            if server_id != b'0':
                btn = QPushButton(self.nodetree)
                btn.server_id = server_id
                btn.setText(hostname)
                btn.setFlat(True)
                btn.setStyleSheet('font-weight:bold')
                icon = bs.resource(bs.settings.gfx_path) / 'icons/addnode.svg'
                btn.setIcon(QIcon(icon.as_posix()))
                btn.setIconSize(QSize(40 if server_id == b'0' else 24, 16))
                btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
                btn.setMaximumHeight(16)
                btn.clicked.connect(self.buttonClicked)
                self.nodetree.setItemWidget(server, 0, btn)

                # Move nodes from ungrouped if they belong to this server
                ungrouped: QTreeWidgetItem = self.servers.get(b'0')
                ucount = 0
                if ungrouped:
                    for node in ungrouped.takeChildren():
                        if node.node_id[:-1] + seqidx2id(0) == server_id:
                            server.addChild(node)
                        else:
                            ungrouped.addChild(node)
                            ucount += 1
                    if not ucount:
                        ungrouped.setHidden(True)
            else:
                self.nodetree.setItemWidget(server, 0, QLabel(hostname, parent=self.nodetree))
            self.servers[server_id] = server


    def nodesChanged(self, node_id):
        if node_id not in self.nodes:
            #print(node_id, 'added to list')
            server_id = node_id[:-1] + seqidx2id(0)
            if server_id not in bs.net.servers:
                server_id = b'0'
            if server_id not in self.servers:
                self.serversChanged(server_id)
            server = self.servers.get(server_id)
            server.setHidden(False)
            node_num = seqid2idx(node_id[-1])
            node = QTreeWidgetItem(server)
            node.setText(0, f'{server.serv_num}:{node_num} <init>')
            node.setText(1, '00:00:00')
            node.node_id  = node_id
            node.node_num = node_num
            node.serv_num = server.serv_num

            self.nodes[node_id] = node

    @subscriber(topic='SHOWDIALOG')
    def on_showdialog_received(self, dialog, args=''):
        ''' Processing of events from simulation nodes. '''
        # dialog = data.get('dialog')
        # args   = data.get('args')
        if dialog == 'OPENFILE':
            self.show_file_dialog()
        elif dialog == 'DOC':
            self.show_doc_window(args)

    @subscriber(topic='SIMINFO')
    def on_siminfo_received(self, speed, simdt, simt, simutc, ntraf, state, scenname):
        simt = tim2txt(simt)[:-3]
        self.setNodeInfo(ctx.sender_id, simt, scenname)
        if ctx.sender_id == bs.net.act_id:
            self.siminfoLabel.setText(u'<b>t:</b> %s, <b>\u0394t:</b> %.2f, <b>Speed:</b> %.1fx, <b>UTC:</b> %s, <b>Mode:</b> %s, <b>Aircraft:</b> %d, <b>Conflicts:</b> %d/%d, <b>LoS:</b> %d/%d'
                % (simt, simdt, speed, simutc, self.modes[state], ntraf, self.nconf_cur, self.nconf_tot, self.nlos_cur, self.nlos_tot))

    def setNodeInfo(self, connid, time, scenname):
        node = self.nodes.get(connid)
        if node:
            node.setText(0, f'{node.serv_num}:{node.node_num} <{scenname}>')
            node.setText(1, time)

    @pyqtSlot(QTreeWidgetItem, int)
    def nodetreeClicked(self, item, column):
        if item in self.servers.values():
            item.setSelected(False)
            item.child(0).setSelected(True)
            bs.net.actnode(item.child(0).node_id)
        else:
            bs.net.actnode(item.node_id)


    @pyqtSlot()
    def buttonClicked(self):
        if self.sender() == self.zoomin:
            self.radarwidget.setpanzoom(zoom=1.4142135623730951, absolute=False)
        elif self.sender() == self.zoomout:
            self.radarwidget.setpanzoom(zoom=0.70710678118654746, absolute=False)
        elif self.sender() == self.pandown:
            self.radarwidget.setpanzoom(pan=(-0.5,  0.0), absolute=False)
        elif self.sender() == self.panup:
            self.radarwidget.setpanzoom(pan=( 0.5,  0.0), absolute=False)
        elif self.sender() == self.panleft:
            self.radarwidget.setpanzoom(pan=( 0.0, -0.5), absolute=False)
        elif self.sender() == self.panright:
            self.radarwidget.setpanzoom(pan=( 0.0,  0.5), absolute=False)
        elif self.sender() == self.ic:
            self.show_file_dialog()
        elif self.sender() == self.sameic:
            stack.stack('IC IC')
        elif self.sender() == self.hold:
            stack.stack('HOLD')
        elif self.sender() == self.op:
            stack.stack('OP')
        elif self.sender() == self.fast:
            stack.stack('FF')
        elif self.sender() == self.fast10:
            stack.stack('FF 0:0:10')
        elif self.sender() == self.showac:
            stack.stack('SHOWTRAF')
        elif self.sender() == self.showpz:
            stack.stack('SHOWPZ')
        elif self.sender() == self.showapt:
            stack.stack('SHOWAPT')
        elif self.sender() == self.showwpt:
            stack.stack('SHOWWPT')
        elif self.sender() == self.showlabels:
            stack.stack('LABEL')
        elif self.sender() == self.showmap:
            stack.stack('SHOWMAP')
        elif self.sender() == self.action_Save:
            stack.stack('SAVEIC')
        elif self.sender() == self.custom1:
            self._open_opensky_dialog()
        elif self.sender() == self.custom2:
            stack.stack('TRACETOGGLE')
        elif self.sender() == self.custom3:
            stack.stack('FUELCALC')
        elif self.sender() == self.custom4:
            stack.stack('CDOGEN')
        elif self.sender() == self.custom5:
            self._open_tmaopt_dialog()
        elif self.sender() == self.custom6:
            self._open_tmarolling_dialog()
        elif hasattr(self.sender(), 'server_id'):
            bs.net.send(b'ADDNODES', 1, self.sender().server_id)

    def show_file_dialog(self, path=None):
        # Due to Qt5 bug in Windows, use temporarily Tkinter
        if platform.system().lower()=='windows':
            fname = fileopen()
        else:
            if path is None or isinstance(path, bool):
                path = bs.resource(bs.settings.scenario_path)

            if isinstance(path, ResourcePath):
                def getscenpath(resource):
                    # Find first path that contains scenario files
                    for p in resource.bases():
                        for f in p.glob('*.[Ss][Cc][Nn]'):
                            if f.name.lower() != 'ic.scn':
                                return p.as_posix()
                    return p.as_posix()
                scenpath = getscenpath(path)
            elif isinstance(path, Path):
                scenpath = path.as_posix()
            else:
                scenpath = path
            
            if platform.system().lower() == 'darwin':
                response = QFileDialog.getOpenFileName(self, 'Open file', scenpath, 'Scenario files (*.scn)')
            else:
                response = QFileDialog.getOpenFileName(self, 'Open file', scenpath, 'Scenario files (*.scn)', options=QFileDialog.Option.DontUseNativeDialog)
            fname = response[0] if isinstance(response, tuple) else response

        # Send IC command to stack with filename if selected, else do nothing
        if fname:
            bs.stack.stack('IC ' + str(fname))

    def show_doc_window(self, cmd=''):
        self.docwin.show_cmd_doc(cmd)
        self.docwin.show()

    def _open_opensky_dialog(self):
        dlg = OpenSkyDialog(self)
        dlg.exec()

    def _open_tmaopt_dialog(self):
        dlg = TMAOptDialog(self)
        dlg.exec()

    def _open_tmarolling_dialog(self):
        dlg = TMARollingDialog(self)
        dlg.exec()
