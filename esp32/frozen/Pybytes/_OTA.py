'''
Copyright (c) 2021, Pycom Limited.
This software is licensed under the GNU GPL version 3 or any
later version, with permitted additional terms. For more information
see the Pycom Licence v1.0 document supplied with this file, or
available at https://www.pycom.io/opensource/licensing
'''
try:
    from pybytes_debug import print_debug
except:
    from _pybytes_debug import print_debug

try:
    from pybytes_constants import constants
except:
    from _pybytes_constants import constants

try:
    import urequest
except:
    import _urequest as urequest

import network
import socket
import ssl
import machine
import ujson
import uhashlib
import ubinascii
import gc
import pycom
import os
from binascii import hexlify

# Try to get version number
# try:
#     from OTA_VERSION import VERSION
# except ImportError:
#     VERSION = '1.0.0'


class OTA():
    # The following two methods need to be implemented in a subclass for the
    # specific transport mechanism e.g. WiFi

    def connect(self):
        raise NotImplementedError()

    def get_data(self, req, dest_path=None, hash=False):
        raise NotImplementedError()

    def update_device_network_config(self, fcota, config):
        raise NotImplementedError()

    # OTA methods

    def get_current_version(self):
        return os.uname().release

    def get_update_manifest(self, fwtype=None, token=None):
        current_version = self.get_current_version()
        sysname = os.uname().sysname
        wmac = hexlify(machine.unique_id()).decode('ascii')
        if fwtype == 'pymesh':
            request_template = "manifest.json?current_ver={}&sysname={}&token={}&ota_slot={}&wmac={}&fwtype={}&current_fwtype={}"
            current_fwtype = 'pymesh' if hasattr(os.uname(), 'pymesh') else 'pybytes'
            req = request_template.format(current_version, sysname, token, hex(pycom.ota_slot()), wmac.upper(), fwtype, current_fwtype)
        elif fwtype == 'pygate':
            request_template = "manifest.json?current_ver={}&sysname={}&ota_slot={}&wmac={}&fwtype={}&current_fwtype={}"
            current_fwtype = 'pygate' if hasattr(os.uname(), 'pygate') else 'pybytes'
            req = request_template.format(current_version, sysname, hex(pycom.ota_slot()), wmac.upper(), fwtype, current_fwtype)
        else:
            request_template = "manifest.json?current_ver={}&sysname={}&wmac={}&ota_slot={}"
            req = request_template.format(current_version, sysname, wmac, hex(pycom.ota_slot()))
        manifest_data = self.get_data(req).decode()
        manifest = ujson.loads(manifest_data)
        gc.collect()
        return manifest

    def update(self, customManifest=None, fwtype=None, token=None):
        try:
            manifest = self.get_update_manifest(fwtype, token) if not customManifest else customManifest
        except Exception as e:
            print('Error reading the manifest, aborting: {}'.format(e))
            return 0

        if manifest is None:
            print("Already on the latest version")
            return 1

        # Download new files and verify hashes
        if "new" in manifest and "update" in manifest:
            for f in manifest['new'] + manifest['update']:
                # Upto 5 retries
                for _ in range(5):
                    try:
                        self.get_file(f)
                        break
                    except Exception as e:
                        print(e)
                        msg = "Error downloading `{}` retrying..."
                        print(msg.format(f['URL']))
                        return 0
                else:
                    raise Exception("Failed to download `{}`".format(f['URL']))

            # Backup old files
            # only once all files have been successfully downloaded
            for f in manifest['update']:
                self.backup_file(f)

            # Rename new files to proper name
            for f in manifest['new'] + manifest['update']:
                new_path = "{}.new".format(f['dst_path'])
                dest_path = "{}".format(f['dst_path'])

                os.rename(new_path, dest_path)

        if "delete" in manifest:
            # `Delete` files no longer required
            # This actually makes a backup of the files incase we need to roll back
            for f in manifest['delete']:
                self.delete_file(f)

        # Flash firmware
        if "firmware" in manifest:
            self.write_firmware(manifest['firmware'])

        # Save version number
        # try:
        #     self.backup_file({"dst_path": "/flash/OTA_VERSION.py"})
        # except OSError:
        #     pass  # There isnt a previous file to backup
        # with open("/flash/OTA_VERSION.py", 'w') as fp:
        #     fp.write("VERSION = '{}'".format(manifest['version']))
        # from OTA_VERSION import VERSION

        return 2

    def get_file(self, f):
        new_path = "{}.new".format(f['dst_path'])
        # If a .new file exists from a previously failed update delete it
        try:
            os.remove(new_path)
        except OSError:
            pass  # The file didnt exist

        # Download new file with a .new extension to not overwrite the existing
        # file until the hash is verified.
        hash = self.get_data(f['URL'].split("/", 3)[-1],
                             dest_path=new_path,
                             hash=True)

        # Hash mismatch
        if hash != f['hash']:
            print(hash, f['hash'])
            msg = "Downloaded file's hash does not match expected hash"
            raise Exception(msg)

    def backup_file(self, f):
        bak_path = "{}.bak".format(f['dst_path'])
        dest_path = "{}".format(f['dst_path'])

        # Delete previous backup if it exists
        try:
            os.remove(bak_path)
        except OSError:
            pass  # There isnt a previous backup

        # Backup current file
        os.rename(dest_path, bak_path)

    def delete_file(self, f):
        bak_path = "/{}.bak_del".format(f)
        dest_path = "/{}".format(f)

        # Delete previous delete backup if it exists
        try:
            os.remove(bak_path)
        except OSError:
            pass  # There isnt a previous delete backup

        # Backup current file
        os.rename(dest_path, bak_path)

    def write_firmware(self, f):
        # hash =
        url = f['URL'].split("//")[1].split("/")[0]

        if url.find(":") > -1:
            self.ip = url.split(":")[0]
            self.port = int(url.split(":")[1])
        else:
            self.ip = url
            self.port = 443

        self.get_data(
            f['URL'].split("/", 3)[-1],
            hash=True,
            firmware=True
        )
        # TODO: Add verification when released in future firmware


class WiFiOTA(OTA):
    def __init__(self, ssid, password, ip, port):
        self.SSID = ssid
        self.password = password
        self.ip = ip
        self.port = port

    def connect(self):
        self.wlan = network.WLAN(mode=network.WLAN.STA)
        if not self.wlan.isconnected() or self.wlan.ssid() != self.SSID:
            for net in self.wlan.scan():
                if net.ssid == self.SSID:
                    self.wlan.connect(self.SSID, auth=(network.WLAN.WPA2,
                                                       self.password))
                    while not self.wlan.isconnected():
                        machine.idle()  # save power while waiting
                    break
            else:
                raise Exception("Cannot find network '{}'".format(self.SSID))
        else:
            # Already connected to the correct WiFi
            pass

    def _http_get(self, path, host):
        req_fmt = 'GET /{} HTTP/1.0\r\nHost: {}\r\n\r\n'
        req = bytes(req_fmt.format(path, host), 'utf8')
        return req

    def get_data(self, req, dest_path=None, hash=False, firmware=False):
        h = None
        useSSL = int(self.port) == 443

        # Connect to server
        print("Requesting: {} to {}:{} with SSL? {}".format(req, self.ip, self.port, useSSL))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        s.connect(socket.getaddrinfo(self.ip, self.port)[0][-1])
        if (int(self.port) == 443):
            print("Wrapping socket")
            s = ssl.wrap_socket(s)
        print("Sending request")
        # Request File
        s.sendall(self._http_get(req, "{}:{}".format(self.ip, self.port)))
        try:
            content = bytearray()
            fp = None
            if dest_path is not None:
                print('dest_path {}'.format(dest_path))
                if firmware:
                    raise Exception("Cannot write firmware to a file")
                fp = open(dest_path, 'wb')

            if firmware:
                print_debug(4, "Starting OTA...")
                pycom.ota_start()

            h = uhashlib.sha1()

            # Get data from server
            result = s.recv(50)
            print_debug(4, "Result: {}".format(result))

            start_writing = False
            while (len(result) > 0):
                # Ignore the HTTP headers
                if not start_writing:
                    if "\r\n\r\n" in result:
                        start_writing = True
                        result = result.split(b'\r\n\r\n')[1]

                if start_writing:
                    if firmware:
                        pycom.ota_write(result)
                    elif fp is None:
                        content.extend(result)
                    else:
                        fp.write(result)

                    if hash:
                        h.update(result)

                result = s.recv(50)

            s.close()

            if fp is not None:
                fp.close()
            if firmware:
                print_debug(6, 'ota_finish')
                pycom.ota_finish()

        except Exception as e:
            gc.mem_free()
            # Since only one hash operation is allowed at Once
            # ensure we close it if there is an error
            if h is not None:
                h.digest()
            raise e

        hash_val = ubinascii.hexlify(h.digest()).decode()

        if dest_path is None:
            if hash:
                return (bytes(content), hash_val)
            else:
                return bytes(content)
        elif hash:
            return hash_val

    def update_device_network_config(self, fcota, config):
        targetURL = '{}://{}/device/networks/{}'.format(
            constants.__DEFAULT_PYCONFIG_PROTOCOL, constants.__DEFAULT_PYCONFIG_DOMAIN, config['device_id']
        )
        print_debug(6, "request device update URL: {}".format(targetURL))
        try:
            pybytes_activation = urequest.get(targetURL, headers={'content-type': 'application/json'})
            responseDetails = pybytes_activation.json()
            pybytes_activation.close()
            print_debug(6, "Response Details: {}".format(responseDetails))
            self.update_network_config(responseDetails, fcota, config)
            machine.reset()
        except Exception as ex:
            print_debug(1, "error while calling {}!: {}".format(targetURL, ex))

    def update_network_config(self, letResp, fcota, config):
        try:
            if 'networkConfig' in letResp:
                netConf = letResp['networkConfig']
                config['network_preferences'] = netConf['networkPreferences']
                if 'wifi' in netConf:
                    config['wifi'] = netConf['wifi']
                elif 'wifi' in config:
                    del config['wifi']

                if 'lte' in netConf:
                    config['lte'] = netConf['lte']
                elif 'lte' in config:
                    del config['lte']

                if 'lora' in netConf:
                    config['lora'] = {
                        'otaa': netConf['lora']['otaa'],
                        'abp': netConf['lora']['abp']
                    }
                elif 'lora' in config:
                    del config['lora']

                json_string = ujson.dumps(config)
                print_debug(1, "update_network_config : {}".format(json_string))
                fcota.update_file_content('/flash/pybytes_config.json', json_string)
        except Exception as e:
            print_debug(1, "error while updating network config pybytes_config.json! {}".format(e))
