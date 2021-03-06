# pylint: disable=missing-docstring
import signal
import subprocess
import sys

# pylint: disable=wrong-import-position,ungrouped-imports
import traceback

import dbus
import dbus.mainloop.glib

from qubesadmin import exc

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)  # isort:skip

import gi
gi.require_version('Gtk', '3.0')  # isort:skip
gi.require_version('AppIndicator3', '0.1')  # isort:skip
from gi.repository import Gtk  # isort:skip
from gi.repository import AppIndicator3 as appindicator  # isort:skip

import qubesadmin

import qui.decorators
import qui.models.qubes

DOMAINS = qui.models.qubes.DomainManager()
LABELS = qui.models.qubes.LabelsManager()
QUBES_APP = qubesadmin.Qubes()

DBUS = dbus.SessionBus()

# TODO Replace pci with usb & mic when they are ready
DEV_TYPES = ['block', 'usb', 'mic']


class DomainMenuItem(Gtk.ImageMenuItem):
    ''' A submenu item for the device menu. Allows attaching and detaching the device to a domain. '''

    def __init__(self, dev: qui.models.qubes.Device,
                 dbus_vm: qui.models.qubes.Domain, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.dbus_vm = dbus_vm
        self.vm = QUBES_APP.domains[str(self.dbus_vm['name'])]

        self.dev = dev
        if self.dev.frontend_domain is None:
            self.attached = False
        elif self.dev.frontend_domain['name'] == self.dbus_vm['name']:
            self.attached = True
        else:
            self.attached = False

        icon = LABELS[self.dbus_vm['label']]['icon']
        self.set_image(qui.decorators.create_icon(icon))
        self._hbox = qui.decorators.device_domain_hbox(self.dbus_vm,
                                                       self.attached)
        self.dev_class = str(self.dev['dev_class'])

        self.add(self._hbox)

        dev_ident = str(self.dev['ident'])

        backend_vm_name = str(self.dev.backend_domain['name'])
        backend_vm = QUBES_APP.domains[backend_vm_name]

        self.assignment = qubesadmin.devices.DeviceAssignment(
            backend_vm, dev_ident, persistent=False)

    def attach(self):
        assert not self.attached
        self.attached = True

        self.remove(self._hbox)
        self._hbox = qui.decorators.device_domain_hbox(self.dbus_vm,
                                                       self.attached)
        self.add(self._hbox)
        self.show_all()

    def detach(self):
        assert self.attached
        self.attached = False
        self.remove(self._hbox)
        self._hbox = qui.decorators.device_domain_hbox(self.dbus_vm,
                                                       self.attached)
        self.add(self._hbox)
        self.show_all()


class DomainMenu(Gtk.Menu):
    def __init__(self, dev: qui.models.qubes.Device, *args, **kwargs):
        super(DomainMenu, self).__init__(*args, **kwargs)
        self.dev = dev
        self.menu_items = {}
        self.attached_item = None

        for vm_obj_path, vm in DOMAINS.children.items():
            if vm_obj_path != dev['backend_domain']\
            and vm['state'] == 'Started'\
            and vm['name'] != 'dom0':
                self.add_vm(None, vm_obj_path)

        DOMAINS.connect_to_signal('Started', self.add_vm)
        DOMAINS.connect_to_signal('DomainAdded', self.refresh_vm_list)
        DOMAINS.connect_to_signal('DomainRemoved', self.refresh_vm_list)
        DOMAINS.connect_to_signal('Halted', self.remove_vm)
        DOMAINS.connect_to_signal('Failed', self.remove_vm)
        DOMAINS.connect_to_signal('Unknown', self.remove_vm)
        self.dev.connect_to_signal('Attached', self.dev_attached)
        self.dev.connect_to_signal('Detached', self.dev_detached)

    def refresh_vm_list(self, *args, **kwargs):
        QUBES_APP.domains.refresh_cache(force=True)

    def add_vm(self, _, obj_path):
        vm = DOMAINS.children[obj_path]
        menu_item = DomainMenuItem(self.dev, vm)
        menu_item.connect('activate', self.toggle)
        if menu_item.attached:
            assert self.attached_item is None,\
                "%s attached to two domains(%s & %s)"\
                % (self.dev['ident'], self.attached_item.dbus_vm['name'], menu_item.dbus_vm['name'])
            self.attached_item = menu_item

        self.menu_items[str(obj_path)] = menu_item

        # sort function
        position = 0
        for i in self.menu_items:
            if str(self.menu_items[i].vm) < str(vm["name"]):
                position += 1

        self.insert(menu_item, position)
        self.show_all()
        self.queue_draw()

    def remove_vm(self, _, vm_obj_path):
        menu_item = self.menu_items[vm_obj_path]
        self.remove(menu_item)
        self.show_all()
        self.queue_draw()

    def dev_attached(self, vm_obj_path):
        menu_item = self.menu_items[vm_obj_path]
        menu_item.attach()
        self.attached_item = menu_item

    def dev_detached(self, vm_obj_path):
        menu_item = self.menu_items[vm_obj_path]
        menu_item.detach()
        self.attached_item = None

    def toggle(self, menu_item):
        if menu_item.attached:
            self.detach()
        else:
            self.attach(menu_item)

    def attach(self, menu_item):
        vm_name = menu_item.vm.name

        if self.attached_item is not None:
            self.detach()

        try:
            menu_item.vm.devices[menu_item.dev_class].attach(
                menu_item.assignment)
            subprocess.call(
                ['notify-send',
                 "Attaching %s to %s" % (self.dev.name, menu_item.vm)])
        except exc.QubesException as ex:
            subprocess.call(
                ['notify-send', '-t', '15000', '-i', 'dialog-error',
                 'Attaching device {0} to {1} failed. Error: {2}'.format(
                     self.dev.name,
                     menu_item.vm,
                     ex)])
        except Exception as ex:
            traceback.print_exc(file=sys.stderr)

    def detach(self):
        menu_item = self.attached_item
        menu_item.vm.devices[menu_item.dev_class].detach(menu_item.assignment)
        vm_name = menu_item.dbus_vm['name']
        subprocess.call([
            'notify-send',
            "Detaching %s from %s" % (self.dev.name, vm_name)
        ])


class DeviceItem(Gtk.ImageMenuItem):
    ''' MenuItem showing the device data and a :class:`DomainMenu`. '''

    def __init__(self, dev_obj_path: dbus.ObjectPath,
                 device_manager, *args, **kwargs):
        "docstring"
        super().__init__(*args, **kwargs)
        self.device_manager = device_manager

        self.dev = \
            self.device_manager[dev_obj_path]  # type: qui.models.qubes.Device
        self.dev_class = self.dev["dev_class"]
        label_path = self.dev.backend_domain['label']  # type: dbus.ObjectPath
        vm_icon = LABELS[label_path]["icon"]  # type: Gtk.Image
        self.hbox = qui.decorators.device_hbox(
            self.dev,
            attached=self.dev.frontend_domain is not None)  # type: Gtk.Box

        self.set_image(qui.decorators.create_icon(vm_icon))
        self.obj_path = dev_obj_path
        self.add(self.hbox)
        submenu = DomainMenu(self.dev)
        self.set_submenu(submenu)

        self.dev.connect_to_signal('Attached', self.attach)
        self.dev.connect_to_signal('Detached', self.detach)
        DOMAINS.connect_to_signal('Halted', self.vm_shutdown)
        DOMAINS.connect_to_signal('Failed', self.vm_shutdown)
        DOMAINS.connect_to_signal('Unknown', self.vm_shutdown)

    def vm_shutdown(self, _, vm_obj_path):
        if self.dev.frontend_domain == DOMAINS.children[vm_obj_path]:
            self.detach(None)

    def attach(self, dev_path):
        self.remove(self.hbox)
        self.hbox = qui.decorators.device_hbox(self.dev, attached=True)
        self.add(self.hbox)
        self.show_all()

    def detach(self, dev_path):
        self.remove(self.hbox)
        self.hbox = qui.decorators.device_hbox(self.dev, attached=False)
        self.add(self.hbox)
        self.show_all()


class DeviceGroups():
    def __init__(self, menu: Gtk.Menu, device_manager):
        self.positions = {}
        self.separators = {}
        self.counters = {}
        self.menu = menu
        self.menu_items = []
        self.device_manager = device_manager

        for pos, dev_type in enumerate(DEV_TYPES):
            self.counters[dev_type] = 0
            if dev_type == DEV_TYPES[0]:
                separator = None
            else:
                separator = Gtk.SeparatorMenuItem()
                self.menu.add(separator)

            self.positions[dev_type] = pos
            self.separators[dev_type] = separator

        self.device_manager.connect_to_signal("Added", self.add)
        self.device_manager.connect_to_signal("Removed", self.remove)

    def add(self, dev_obj_path: dbus.ObjectPath):
        dev = self.device_manager[dev_obj_path]
        if dev['dev_class'] not in DEV_TYPES:
            return

        position = self._position(dev['dev_class'])

        #sort function
        name = dev.backend_domain['name'] + ':' + dev['ident']
        for i in self.menu_items:
            if str(dev['dev_class']) == str(i.dev['dev_class']):
                if str(i.dev.backend_domain['name'] + ':' + i.dev['ident']) <= str(name):
                    position += 1
        self._insert(dev_obj_path, position)

        if dev['dev_class'] not in [DEV_TYPES[0], DEV_TYPES[-1]]:
            self.separators[dev['dev_class']].show()

        subprocess.call(['notify-send', "Device %s is available" % (dev.name)])

    def _position(self, dev_type):
        if dev_type == DEV_TYPES[0]:
            return 0
        else:
            return self.positions[dev_type] - self.counters[dev_type] + 1

    def _insert(self, dev_obj_path: dbus.ObjectPath, position: int) -> None:
        dev = self.device_manager[dev_obj_path]
        menu_item = DeviceItem(dev_obj_path, self.device_manager)
        self.menu.insert(menu_item, position)
        self.counters[dev["dev_class"]] += 1
        self.menu_items.append(menu_item)
        self._shift_positions(dev["dev_class"])
        self._recalc_separators()
        menu_item.show_all()

    def remove(self, dev_obj_path: dbus.ObjectPath):
        for item in self.menu_items:
            if item.obj_path == dev_obj_path:
                self.menu.remove(item)
                self.menu_items.remove(item)
                self.counters[item.dev_class] -= 1
                self._unshift_positions(item.dev_class)
                self._recalc_separators()
                subprocess.call(
                    ['notify-send',
                     "Device %s is removed" % (item.dev.name)])
                return

    def _recalc_separators(self):
        for dev_type, size in self.counters.items():
            separator = self.separators[dev_type]
            if separator is not None:
                if size > 0:
                    separator.show()
                else:
                    separator.hide()

    def _shift_positions(self, dev_type):
        if dev_type == DEV_TYPES[-1]:
            return

        start_index = DEV_TYPES.index(dev_type)
        index_to_update = DEV_TYPES[start_index:]

        for index in index_to_update:
            self.positions[index] += 1

    def _unshift_positions(self, dev_type):
        if dev_type in [DEV_TYPES[0], DEV_TYPES[-1]]:
            return

        for index in DEV_TYPES[1:]:
            assert self.positions[index] > 0
            self.positions[index] -= 1


class DevicesTray(Gtk.Application):
    def __init__(self, app_name='Devices Tray'):
        super(DevicesTray, self).__init__()
        self.name = app_name
        self.tray_menu = Gtk.Menu()
        self.device_manager = qui.models.qubes.DevicesManager()
        self.devices = DeviceGroups(self.tray_menu, self.device_manager)

        self.ind = appindicator.Indicator.new(
            'Devices Widget', "media-removable",
            appindicator.IndicatorCategory.SYSTEM_SERVICES)
        self.ind.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.ind.set_menu(self.tray_menu)

    def run(self):  # pylint: disable=arguments-differ
        for obj_path in self.device_manager.children:
            self.devices.add(obj_path)

        self.tray_menu.show_all()

        Gtk.main()


def main():
    app = DevicesTray()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app.run()


if __name__ == '__main__':
    sys.exit(main())
