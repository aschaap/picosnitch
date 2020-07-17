# MIT License

# Copyright (c) 2020 Eric Lesiuta

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import ipaddress
import json
import multiprocessing
import os
import signal
import sys
import time
import typing

import plyer
import psutil


def read() -> dict:
    file_path = os.path.join(os.path.expanduser("~"), ".config", "picosnitch", "snitch.json")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8", errors="surrogateescape") as json_file:
            data = json.load(json_file)
        assert all(key in data for key in ["Config", "Errors", "Latest Entries", "Names", "Processes", "Remote Addresses"])
        return data
    return {
        "Config": {"Polling interval": 0.2, "Write interval": 600, "Use pcap": False, "Remote Addresses ignored ports": [80, 443]},
        "Errors": [],
        "Latest Entries": [],
        "Names": {},
        "Processes": {},
        "Remote Addresses": {}
        }


def write(snitch: dict):
    file_path = os.path.join(os.path.expanduser("~"), ".config", "picosnitch", "snitch.json")
    if not os.path.isdir(os.path.dirname(file_path)):
        os.makedirs(os.path.dirname(file_path))
    try:
        with open(file_path, "w", encoding="utf-8", errors="surrogateescape") as json_file:
            json.dump(snitch, json_file, indent=2, separators=(',', ': '), sort_keys=True, ensure_ascii=False)
    except Exception:
        toast("picosnitch write error", file=sys.stderr)


def terminate(snitch: dict, process: multiprocessing.Process = None):
    write(snitch)
    if process is not None:
        process.terminate()
    sys.exit(0)


def poll(snitch: dict, last_connections: set, pcap_dict: dict) -> set:
    ctime = time.ctime()
    proc = {"name": "", "exe": "", "cmdline": "", "pid": ""}
    current_connections = set(psutil.net_connections(kind="all"))
    for conn in current_connections - last_connections:
        try:
            if conn.pid is not None and conn.raddr and not ipaddress.ip_address(conn.raddr.ip).is_private:
                _ = pcap_dict.pop(str(conn.laddr.port) + str(conn.raddr.ip), None)
                proc = psutil.Process(conn.pid).as_dict(attrs=["name", "exe", "cmdline", "pid"], ad_value="")
                if proc["exe"] not in snitch["Processes"]:
                    new_entry(snitch, proc, conn, ctime)
                else:
                    update_entry(snitch, proc, conn, ctime)
        except Exception:
            error = str(conn)
            if conn.pid == proc["pid"]:
                error += str(proc["pid"])
            else:
                error += "{process no longer exists}"
            snitch["Errors"].append(ctime + " " + error)
            toast("picosnitch polling error: " + error, file=sys.stderr)
    for conn in pcap_dict:
        snitch["Errors"].append(ctime + " " + str(conn))
        toast("picosnitch missed connection: " + str(conn), file=sys.stderr)
    return current_connections


def new_entry(snitch: dict, proc: dict, conn, ctime: str):
    # Update Latest Entries
    snitch["Latest Entries"].insert(0, proc["name"] + " - " + proc["exe"])
    # Update Names
    if proc["name"] in snitch["Names"]:
        if proc["exe"] not in snitch["Names"][proc["name"]]:
            snitch["Names"][proc["name"]].append(proc["exe"])
    else:
        snitch["Names"][proc["name"]] = [proc["exe"]]
    # Update Processes
    snitch["Processes"][proc["exe"]] = {
        "name": proc["name"],
        "cmdlines": [str(proc["cmdline"])],
        "first seen": ctime,
        "last seen": ctime,
        "days seen": 1,
        "remote addresses": []
    }
    # Update Remote Addresses
    if conn.laddr.port not in snitch["Config"]["Remote Addresses ignored ports"]:
        snitch["Processes"][proc["exe"]]["remote addresses"].append(conn.raddr.ip)
        if conn.raddr.ip in snitch["Remote Addresses"]:
            if proc["exe"] not in snitch["Remote Addresses"][conn.raddr.ip]:
                snitch["Remote Addresses"][conn.raddr.ip].append(proc["exe"])
        else:
            snitch["Remote Addresses"][conn.raddr.ip] = [proc["exe"]]
    # Notify
    toast("First network connection detected for " + proc["name"])


def update_entry(snitch: dict, proc: dict, conn, ctime: str):
    entry = snitch["Processes"][proc["exe"]]
    if proc["name"] not in entry["name"]:
        entry["name"] += " alternative=" + proc["name"]
    if str(proc["cmdline"]) not in entry["cmdlines"]:
        entry["cmdlines"].append(str(proc["cmdline"]))
    if conn.raddr.ip not in entry["remote addresses"] and conn.laddr.port not in snitch["Config"]["Remote Addresses ignored ports"]:
        entry["remote addresses"].append(conn.raddr.ip)
    if ctime.split()[:3] != entry["last seen"].split()[:3]:
        entry["days seen"] += 1
    entry["last seen"] = ctime


def loop():
    snitch = read()
    p_sniff, q_packet, q_error = init_pcap(snitch)
    pcap_dict = {}
    signal.signal(signal.SIGTERM, lambda *args: terminate(snitch, p_sniff))
    signal.signal(signal.SIGINT, lambda *args: terminate(snitch, p_sniff))
    connections = set()
    polling_interval = snitch["Config"]["Polling interval"]
    write_counter = int(snitch["Config"]["Write interval"] / polling_interval)
    counter = 0
    while True:
        if q_packet is not None:
            pcap_dict = {}
            known_ports = [conn.laddr.port for conn in connections]
            known_raddr = [conn.raddr.ip for conn in connections]
            while not q_packet.empty():
                packet = q_packet.get()
                if not (packet["laddr_port"] in known_ports or packet["raddr_ip"] in known_raddr):
                    pcap_dict[str(packet["laddr_port"]) + str(packet["raddr_ip"])] = packet
            if not q_error.empty():
                toast(q_error.get())
                p_sniff.terminate()
                p_sniff.close()
                p_sniff, q_packet, q_error = init_pcap(snitch)
        connections = poll(snitch, connections, pcap_dict)
        time.sleep(polling_interval)
        if counter >= write_counter:
            write(snitch)
            counter = 0
        else:
            counter += 1


def toast(msg: str, file=sys.stdout):
    try:
        plyer.notification.notify(title="picosnitch",
                                  message=msg,
                                  app_name="picosnitch")
    except Exception:
        print(msg, file=file)


def init_pcap(snitch: dict) -> typing.Tuple[multiprocessing.Process, multiprocessing.Queue]:
    if snitch["Config"]["Use pcap"]:
        import scapy
        from scapy.all import sniff

        def parse_packet(packet) -> dict:
            output = {"proto": packet.proto, "laddr_port": None}
            # output["packet"] = str(packet.show(dump=True))
            src = packet.getlayer(scapy.layers.all.IP).src
            dst = packet.getlayer(scapy.layers.all.IP).dst
            if ipaddress.ip_address(src).is_private:
                output["direction"] = "outgoing"
                output["laddr_ip"], output["raddr_ip"] = src, dst
                if hasattr(packet, "sport"):
                    output["laddr_port"] = packet.sport
            elif ipaddress.ip_address(dst).is_private:
                output["direction"] = "incoming"
                output["laddr_ip"], output["raddr_ip"] = dst, src
                if hasattr(packet, "dport"):
                    output["laddr_port"] = packet.dport
            return output

        def filter_packet(packet) -> bool:
            try:
                src = ipaddress.ip_address(packet.getlayer(scapy.layers.all.IP).src)
                dst = ipaddress.ip_address(packet.getlayer(scapy.layers.all.IP).dst)
                return src.is_private != dst.is_private
            except:
                return False

        def sniffer(q_packet, q_error):
            try:
                sniff(count=0, prn=lambda x: q_packet.put(parse_packet(x)), lfilter=filter_packet)
            except Exception as e:
                q_error.put("picosnitch sniffer exception: " + str(e))

        if __name__ == "__main__":
            q_packet = multiprocessing.Queue()
            q_error = multiprocessing.Queue()
            p_sniff = multiprocessing.Process(target=sniffer, args=(q_packet, q_error))
            p_sniff.start()
            print("pcap started")
            return p_sniff, q_packet, q_error
    print("pcap failed")
    return None, None, None


def main():
    # if os.name == "posix":
    #     import daemon
    #     with daemon.DaemonContext():
    #         loop()
    # else:
    loop()


if __name__ == "__main__":
    sys.exit(main())
