"""Προσωρινό παράθυρο δοκιμών Director. Χωρίς αλλαγή πρωτοκόλλου.

Το tkinter τρέχει στο main thread. Οι εργασίες δικτύου/μικροφώνου παραμένουν
στα υπάρχοντα worker threads. Δεν καλούνται tkinter widgets από workers.
"""
from __future__ import annotations

import socket
import threading
import time

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from shared.discovery import announce, director_id


MIC_RETRY_SECONDS = 2.0


def run_microphone(stream_microphone, broadcaster, *, stop: threading.Event,
                   ready: threading.Event, set_error,
                   retry_seconds: float = MIC_RETRY_SECONDS, **kwargs) -> None:
    """Τρέχει το μικρόφωνο μέχρι το stop και ξαναδοκιμάζει μετά από σφάλμα.

    Σφάλμα μικροφώνου ΔΕΝ σταματά discovery/accept: οι Clients συνεχίζουν να
    βρίσκουν και να συνδέονται στον Director. Το GUI βλέπει το σφάλμα και
    σταματά την ανακοίνωση· μόλις το μικρόφωνο ανοίξει ξανά, το σφάλμα σβήνει.
    """
    def on_ready() -> None:
        set_error(None)
        ready.set()

    while not stop.is_set():
        try:
            stream_microphone(broadcaster, stop=stop, ready_callback=on_ready, **kwargs)
        except Exception as exc:
            ready.clear()
            set_error(str(exc) or exc.__class__.__name__)
        else:
            ready.clear()
        stop.wait(retry_seconds)


class AnnouncementControls:
    """Ελάχιστος, ελέγξιμος χειρισμός στόχων και toggle χωρίς dependency GUI."""

    def __init__(self, broadcaster: AudioBroadcaster):
        self.broadcaster = broadcaster
        self.active = False
        self.target_rooms: set[str] | None = None
        # Κρίσιμο: ο broadcaster επιλέγει εξ ορισμού *όλους*.
        # Το GUI ΠΟΤΕ δεν ξεκινά με ενεργή ανακοίνωση.
        self.broadcaster.set_targets(rooms=set(), client_ids=set())

    def start(self, rooms: set[str] | None = None) -> None:
        if self.active:
            return
        if rooms is not None and not rooms:
            raise ValueError("Επίλεξε τουλάχιστον μία αίθουσα.")
        # Η επιλογή κατά την ανακοίνωση παραμένει σταθερή μέχρι τη λήξη.
        self.broadcaster.set_targets(rooms=rooms, client_ids=set() if rooms is not None else None)
        self.target_rooms = None if rooms is None else set(rooms)
        self.active = True

    def stop(self) -> None:
        # Idempotent: ασφαλής κλήση και κατά το κλείσιμο του παραθύρου.
        self.broadcaster.set_targets(rooms=set(), client_ids=set())
        self.active = False
        self.target_rooms = None


def summarize_rooms(status):
    """Ταυτόχρονοι Clients στην ίδια αίθουσα μετρώνται ξεχωριστά."""
    rooms = {}
    for client_id, room, online, age in status:
        counts = rooms.setdefault(room, [0, 0])
        counts[0 if online else 1] += 1
    return [(room, online, offline) for room, (online, offline) in sorted(rooms.items())]


def run_gui(args, *, accept_clients, stream_microphone) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("School Intercom — Director (δοκιμές)")
    root.minsize(680, 560)
    root.geometry("820x650")

    stop = threading.Event()
    broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS,
                                   on_event=lambda s: print(s, flush=True))
    controls = AnnouncementControls(broadcaster)
    identifier = director_id(args.director_id_file)
    threads = []
    peak_lock = threading.Lock()
    peak = [0.0, 0.0]  # στιγμιαία στάθμη / monotonic τελευταίας λήψης
    mic_error = [None]
    mic_ready = threading.Event()

    def update_peak(value: float) -> None:
        with peak_lock:
            peak[:] = [value, time.monotonic()]

    def set_mic_error(message: str | None) -> None:
        mic_error[0] = message

    def mic_worker() -> None:
        run_microphone(stream_microphone, broadcaster, stop=stop, ready=mic_ready,
                       set_error=set_mic_error, device=args.device,
                       latency=args.latency, level_callback=update_peak)

    main = ttk.Frame(root, padding=20)
    main.pack(fill="both", expand=True)
    heading = ttk.Label(main, text="School Intercom", font=("Sans", 20, "bold"))
    heading.pack(anchor="w")
    ttk.Label(main, text="Director · Προσωρινό περιβάλλον πραγματικών δοκιμών").pack(anchor="w", pady=(0, 18))

    state_var = tk.StringVar(value="Αναμονή — δεν μεταδίδεται φωνή")
    state_label = ttk.Label(main, textvariable=state_var, font=("Sans", 13, "bold"))
    state_label.pack(anchor="w", pady=(0, 10))

    choice = tk.StringVar(value="all")
    recipient_frame = ttk.LabelFrame(main, text="Παραλήπτες", padding=12)
    recipient_frame.pack(fill="x", pady=(0, 14))
    radio_all = ttk.Radiobutton(recipient_frame, text="Όλες οι διαθέσιμες αίθουσες",
                                variable=choice, value="all")
    radio_all.pack(anchor="w")
    radio_rooms = ttk.Radiobutton(recipient_frame, text="Επιλεγμένες αίθουσες",
                                  variable=choice, value="rooms")
    radio_rooms.pack(anchor="w", pady=(5, 4))
    room_list = tk.Listbox(recipient_frame, height=5, selectmode="multiple",
                           exportselection=False)
    room_list.pack(fill="x", pady=(2, 0))
    ttk.Label(recipient_frame, text="Ctrl/Shift + κλικ για πολλαπλή επιλογή. "
              "Η επιλογή κλειδώνει όσο διαρκεί η ανακοίνωση.").pack(anchor="w", pady=(5, 0))

    announce_button = tk.Button(main, text="Έναρξη ανακοίνωσης", font=("Sans", 15, "bold"),
                                padx=18, pady=12, bg="#165b40", fg="white",
                                activebackground="#237450", activeforeground="white")
    announce_button.pack(fill="x", pady=(0, 16))

    meter_frame = ttk.LabelFrame(main, text="Στάθμη μικροφώνου", padding=10)
    meter_frame.pack(fill="x", pady=(0, 16))
    meter = ttk.Progressbar(meter_frame, orient="horizontal", maximum=100)
    meter.pack(fill="x")
    meter_label = ttk.Label(meter_frame, text="Μικρόφωνο: αναμονή")
    meter_label.pack(anchor="w", pady=(5, 0))

    clients_frame = ttk.LabelFrame(main, text="Συνδεδεμένοι Clients", padding=10)
    clients_frame.pack(fill="both", expand=True)
    count_var = tk.StringVar(value="Online: 0 · Offline: 0")
    ttk.Label(clients_frame, textvariable=count_var).pack(anchor="w")
    tree = ttk.Treeview(clients_frame, columns=("room", "state", "heartbeat"), show="headings", height=7)
    tree.heading("room", text="Αίθουσα")
    tree.heading("state", text="Κατάσταση")
    tree.heading("heartbeat", text="Τελευταίο heartbeat")
    tree.column("room", width=180)
    tree.column("state", width=110)
    tree.column("heartbeat", width=185)
    tree.pack(fill="both", expand=True, pady=(6, 0))

    room_names = []

    def toggle():
        try:
            if controls.active:
                controls.stop()
            else:
                if not mic_ready.is_set() or mic_error[0]:
                    messagebox.showwarning("Μικρόφωνο", "Το μικρόφωνο δεν είναι διαθέσιμο.")
                    return
                status = broadcaster.status_snapshot()
                online_rooms = {room for _, room, online, _ in status if online}
                if not online_rooms:
                    messagebox.showwarning("Παραλήπτες", "Δεν υπάρχει συνδεδεμένος Client.")
                    return
                if choice.get() == "all":
                    controls.start()
                else:
                    selected = {room_names[i] for i in room_list.curselection()}
                    if not selected:
                        messagebox.showwarning("Παραλήπτες", "Επίλεξε τουλάχιστον μία αίθουσα.")
                        return
                    if not (selected & online_rooms):
                        messagebox.showwarning("Παραλήπτες", "Καμία επιλεγμένη αίθουσα δεν έχει Online Client.")
                        return
                    controls.start(selected)
        except Exception as exc:
            messagebox.showerror("Ανακοίνωση", str(exc))
        redraw_state()

    announce_button.configure(command=toggle)

    def redraw_state():
        if controls.active:
            state_var.set("ΑΝΑΚΟΙΝΩΣΗ ΣΕ ΕΞΕΛΙΞΗ")
            announce_button.configure(text="Λήξη ανακοίνωσης", bg="#ae3333",
                                      activebackground="#c94444")
        else:
            state_var.set("Αναμονή — δεν μεταδίδεται φωνή")
            announce_button.configure(text="Έναρξη ανακοίνωσης", bg="#165b40",
                                      activebackground="#237450")
        room_list.configure(state="disabled" if controls.active else "normal")
        radio_all.configure(state="disabled" if controls.active else "normal")
        radio_rooms.configure(state="disabled" if controls.active else "normal")

    def refresh():
        if not root.winfo_exists():
            return
        status = broadcaster.status_snapshot()
        online = sum(1 for _, _, is_online, _ in status if is_online)
        count_var.set(f"Online: {online} · Offline: {len(status) - online}")
        existing = set(tree.get_children())
        current = set()
        for uid, room, is_online, age in status:
            current.add(uid)
            values = (room, "Online" if is_online else "Offline",
                      f"{age:.1f} s πριν")
            if uid in existing:
                tree.item(uid, values=values)
            else:
                tree.insert("", "end", iid=uid, values=values)
        for uid in existing - current:
            tree.delete(uid)

        names = [room for room, online_count, offline_count in summarize_rooms(status)]
        if names != room_names and not controls.active:
            selected = {room_names[i] for i in room_list.curselection()}
            room_names[:] = names
            room_list.delete(0, "end")
            for index, room in enumerate(room_names):
                count = next(online_count for name, online_count, _ in summarize_rooms(status) if name == room)
                room_list.insert("end", f"{room} · Online: {count}")
                if room in selected:
                    room_list.selection_set(index)

        with peak_lock:
            value, last = peak
        if time.monotonic() - last > 0.6:
            value = 0.0
        meter.configure(value=min(100, value * 100))
        if mic_error[0]:
            meter_label.configure(text=f"Σφάλμα μικροφώνου: {mic_error[0]} — νέα προσπάθεια σε λίγο")
            if controls.active:
                controls.stop()
                redraw_state()
        else:
            meter_label.configure(text="Μικρόφωνο: " +
                                  ("ενεργό" if mic_ready.is_set() else "αναμονή"))
        root.after(250, refresh)

    def on_close():
        controls.stop()
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(MAX_CLIENTS)
            for name, target, kwargs in [
                ("discovery", announce, {"args": (stop, identifier, args.port),
                                         "kwargs": {"udp_port": args.discovery_port}}),
                ("accept", accept_clients, {"args": (server, broadcaster, stop)}),
                ("microphone", mic_worker, {"args": ()}),
            ]:
                thread = threading.Thread(target=target, daemon=True,
                                          name=f"director-gui-{name}", **kwargs)
                thread.start()
                threads.append(thread)
            refresh()
            root.mainloop()
    finally:
        # Πρώτα STOP στους Clients, μετά κλείσιμο sockets και worker threads.
        controls.stop()
        stop.set()
        broadcaster.close()
        for thread in threads:
            thread.join(timeout=1)
