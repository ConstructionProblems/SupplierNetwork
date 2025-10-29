import time
import tkinter as tk
from tkinter import ttk, messagebox

# ----------------------------
# Grid Survival Control Panel
# ----------------------------
# Features:
# - Start / Pause / Reset countdown
# - Depletion rates: baseline (1/45 pt per second), Half, Normal, 2x
# - Station +2, MUS +7
# - Edit start grid and duration; apply while paused
# - Fullscreen toggle (F), Space to Start/Pause
# - Big readable UI for projection
# ----------------------------

class GridApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Grid Survival – Live Grid Meter")
        self.geometry("1100x720")
        self.configure(bg="#0b1220")

        # --- Core state ---
        self.start_grid_points = 15          # default starting grid
        self.grid_points = float(self.start_grid_points)

        self.duration_sec = 30 * 60          # default 30 minutes
        self.elapsed_sec = 0.0

        self.running = False

        # Depletion: baseline = 1 point per 45s
        self.baseline_rate_pts_per_sec = 1.0 / 45.0
        self.rate_multiplier = 1.0           # 0.5 (half), 1.0 (normal), 2.0 (double)

        self.last_tick_ms = None

        # Team tracking
        self.team_station_counts = [0] * 10
        self.team_mus_counts = [0] * 10
        self.team_station_labels = []
        self.team_mus_labels = []
        self.team_station_points_labels = []
        self.team_mus_points_labels = []
        self.team_total_points_labels = []
        self.team_container = None
        self._team_scroll_canvas = None

        # --- UI ---
        self._build_ui()
        self._init_sentiments()
        self.alert_flicker_job = None
        self.alert_flicker_state = True
        self.alert_flicker_rate = None
        self.alert_flag_visible_color = "#19e68c"
        self.current_alert_signature = None
        self.alert_state = "none"
        self.victory_active = False
        self._set_alert_flag("GRID ONLINE", "#19e68c")
        self._tick()  # start UI loop

        # --- Keybinds ---
        self.bind("<space>", lambda e: self.toggle_start_pause())
        self.bind("<Escape>", lambda e: self.exit_fullscreen())
        self.bind("f", lambda e: self.toggle_fullscreen())
        self.bind("F", lambda e: self.toggle_fullscreen())
        self.bind("r", lambda e: self.reset())
        self.bind("1", lambda e: self.set_half_rate())
        self.bind("2", lambda e: self.set_normal_rate())
        self.bind("3", lambda e: self.set_double_rate())
        self.bind("s", lambda e: self.add_station())
        self.bind("m", lambda e: self.add_mus())

    # ---------- UI BUILD ----------
    def _build_ui(self):
        # Top display
        self.banner_label = tk.Label(
            self, text="GRID STABILITY", font=("Helvetica", 44, "bold"),
            fg="#e6f1ff", bg="#0b1220"
        )
        self.banner_label.pack(pady=(18, 0))

        self.alert_flag = tk.Label(
            self, text="", font=("Helvetica", 60, "bold"),
            fg="#ff5c5c", bg="#0b1220"
        )
        self.alert_flag.pack(pady=(0, 10))

        self.public_label = tk.Label(
            self,
            text="All lights on and everyone happy.",
            font=("Helvetica", 20, "bold"),
            fg="#8be9fd",
            bg="#0b1220"
        )
        self.public_label.pack(pady=(0, 18))

        self.victory_label = tk.Label(
            self,
            text="",
            font=("Helvetica", 48, "bold"),
            fg="#19e68c",
            bg="#0b1220",
            justify="center"
        )
        self.victory_label.pack(pady=(0, 12))

        self.header = tk.Frame(self, bg="#0b1220")
        self.header.pack(fill="x", pady=(10, 10))

        self.grid_label = tk.Label(
            self.header, text=f"{int(round(self.grid_points))}",
            font=("Helvetica", 140, "bold"), fg="#19e68c", bg="#0b1220"
        )
        self.grid_label.pack(side="left", padx=30)

        right_panel = tk.Frame(self.header, bg="#0b1220")
        right_panel.pack(side="left", expand=True, fill="both")

        self.status_label = tk.Label(
            right_panel, text="PAUSED", font=("Helvetica", 28, "bold"),
            fg="#ffe08a", bg="#0b1220"
        )
        self.status_label.pack(anchor="w", pady=(10, 5))

        self.timer_label = tk.Label(
            right_panel, text=self._fmt_time(self.duration_sec - self.elapsed_sec),
            font=("Helvetica", 60, "bold"), fg="#e6f1ff", bg="#0b1220"
        )
        self.timer_label.pack(anchor="w")

        self.rate_label = tk.Label(
            right_panel, text=self._rate_text(),
            font=("Helvetica", 20), fg="#a0b3c5", bg="#0b1220"
        )
        self.rate_label.pack(anchor="w", pady=(8,0))

        # Controls
        controls = tk.Frame(self, bg="#0b1220")
        controls.pack(fill="x", pady=10)

        # Start/Pause/Reset
        sp = tk.Frame(controls, bg="#0b1220")
        sp.pack(side="left", padx=15)
        self.start_btn = ttk.Button(sp, text="▶ Start", command=self.start)
        self.start_btn.grid(row=0, column=0, padx=4, pady=4)
        self.pause_btn = ttk.Button(sp, text="⏸ Pause", command=self.pause)
        self.pause_btn.grid(row=0, column=1, padx=4, pady=4)
        self.reset_btn = ttk.Button(sp, text="↺ Reset", command=self.reset)
        self.reset_btn.grid(row=0, column=2, padx=4, pady=4)

        # Rate buttons
        rates = tk.Frame(controls, bg="#0b1220")
        rates.pack(side="left", padx=30)
        ttk.Label(rates, text="Depletion Rate:", font=("Helvetica", 12)).grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Button(rates, text="¼x", width=6, command=self.set_quarter_rate).grid(row=1, column=0, padx=3, pady=4)
        ttk.Button(rates, text="½x", width=6, command=self.set_half_rate).grid(row=1, column=1, padx=3, pady=4)
        ttk.Button(rates, text="1x", width=6, command=self.set_normal_rate).grid(row=1, column=2, padx=3, pady=4)
        ttk.Button(rates, text="2x", width=6, command=self.set_double_rate).grid(row=1, column=3, padx=3, pady=4)
        ttk.Button(rates, text="4x", width=6, command=self.set_quad_rate).grid(row=1, column=4, padx=3, pady=4)
        ttk.Button(rates, text="0x (Pause)", command=self.set_paused_rate).grid(row=2, column=0, columnspan=5, padx=3, pady=4, sticky="we")

        # Events (Points)
        events = tk.Frame(controls, bg="#0b1220")
        events.pack(side="left", padx=30)
        ttk.Label(events, text="Adjust Stability:", font=("Helvetica", 12)).grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Button(events, text="-1", width=7, command=lambda: self.add_points(-1)).grid(row=1, column=0, padx=3, pady=4)
        ttk.Button(events, text="+1", width=7, command=lambda: self.add_points(1)).grid(row=1, column=1, padx=3, pady=4)
        ttk.Button(events, text="+5", width=7, command=lambda: self.add_points(5)).grid(row=1, column=2, padx=3, pady=4)
        ttk.Button(events, text="+2 Station (S)", command=self.add_station).grid(row=2, column=0, columnspan=2, padx=3, pady=4, sticky="we")
        ttk.Button(events, text="+7 MUS (M)", command=self.add_mus).grid(row=2, column=2, columnspan=2, padx=3, pady=4, sticky="we")
        ttk.Button(events, text="Victory!", command=self.show_victory_effect).grid(row=3, column=0, columnspan=2, padx=3, pady=4, sticky="we")
        ttk.Button(events, text="Undo Victory", command=self.clear_victory_effect).grid(row=3, column=2, columnspan=2, padx=3, pady=4, sticky="we")

        # Team trackers (scrollable)
        team_container = tk.Frame(self, bg="#0b1220")
        team_container.pack(fill="x", padx=16, pady=(8, 12))
        canvas = tk.Canvas(team_container, bg="#0b1220", highlightthickness=0, height=220)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(team_container, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        team_frame = tk.Frame(canvas, bg="#0b1220")
        canvas.create_window((0, 0), window=team_frame, anchor="nw")
        team_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        ttk.Label(team_frame, text="Team Energization Tracker", font=("Helvetica", 14, "bold")).grid(row=0, column=0, columnspan=10, sticky="w", pady=(0,4))
        headers = [
            "Team", "Station -", "Station Count", "Station +", "Station pts",
            "MUS -", "MUS Count", "MUS +", "MUS pts", "Total pts"
        ]
        for idx, hdr in enumerate(headers):
            ttk.Label(team_frame, text=hdr, font=("Helvetica", 11, "bold")).grid(row=1, column=idx, padx=4, pady=2)

        self.team_station_labels = []
        self.team_mus_labels = []
        self.team_station_points_labels = []
        self.team_mus_points_labels = []
        self.team_total_points_labels = []

        for team_idx in range(10):
            row = team_idx + 2
            ttk.Label(team_frame, text=f"Team {team_idx + 1}", width=10).grid(row=row, column=0, padx=4, pady=2, sticky="w")
            ttk.Button(team_frame, text="−", width=6,
                       command=lambda i=team_idx: self.update_team_station(i, -1)).grid(row=row, column=1, padx=2, pady=2)
            station_label = ttk.Label(team_frame, text="0", width=10, anchor="center")
            station_label.grid(row=row, column=2, padx=2, pady=2)
            ttk.Button(team_frame, text="+", width=6,
                       command=lambda i=team_idx: self.update_team_station(i, 1)).grid(row=row, column=3, padx=2, pady=2)
            station_pts_label = ttk.Label(team_frame, text="0 pts", width=10, anchor="center")
            station_pts_label.grid(row=row, column=4, padx=2, pady=2)

            ttk.Button(team_frame, text="−", width=6,
                       command=lambda i=team_idx: self.update_team_mus(i, -1)).grid(row=row, column=5, padx=2, pady=2)
            mus_label = ttk.Label(team_frame, text="0", width=10, anchor="center")
            mus_label.grid(row=row, column=6, padx=2, pady=2)
            ttk.Button(team_frame, text="+", width=6,
                       command=lambda i=team_idx: self.update_team_mus(i, 1)).grid(row=row, column=7, padx=2, pady=2)
            mus_pts_label = ttk.Label(team_frame, text="0 pts", width=10, anchor="center")
            mus_pts_label.grid(row=row, column=8, padx=2, pady=2)

            total_pts_label = ttk.Label(team_frame, text="0 pts", width=12, anchor="center")
            total_pts_label.grid(row=row, column=9, padx=4, pady=2)

            self.team_station_labels.append(station_label)
            self.team_mus_labels.append(mus_label)
            self.team_station_points_labels.append(station_pts_label)
            self.team_mus_points_labels.append(mus_pts_label)
            self.team_total_points_labels.append(total_pts_label)

        totals_row = 12
        ttk.Separator(team_frame, orient="horizontal").grid(row=totals_row, column=0, columnspan=10, sticky="ew", pady=(6,4))
        totals_row += 1
        ttk.Label(team_frame, text="Totals", font=("Helvetica", 11, "bold")).grid(row=totals_row, column=0, padx=4, pady=2, sticky="w")
        self.total_station_count_label = ttk.Label(team_frame, text="0")
        self.total_station_count_label.grid(row=totals_row, column=2, padx=2, pady=2)
        self.total_station_points_label = ttk.Label(team_frame, text="0 pts")
        self.total_station_points_label.grid(row=totals_row, column=4, padx=2, pady=2)
        self.total_mus_count_label = ttk.Label(team_frame, text="0")
        self.total_mus_count_label.grid(row=totals_row, column=6, padx=2, pady=2)
        self.total_mus_points_label = ttk.Label(team_frame, text="0 pts")
        self.total_mus_points_label.grid(row=totals_row, column=8, padx=2, pady=2)
        self.total_points_label = ttk.Label(team_frame, text="0 pts")
        self.total_points_label.grid(row=totals_row, column=9, padx=4, pady=2)
        totals_row += 1
        ttk.Label(team_frame, text="Total Points Added:", font=("Helvetica", 11, "bold")).grid(row=totals_row, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        self.total_points_detail_label = ttk.Label(team_frame, text="0 pts")
        self.total_points_detail_label.grid(row=totals_row, column=2, columnspan=2, padx=2, pady=4, sticky="w")
        self._refresh_team_totals()
        self.team_container = team_container
        self._team_scroll_canvas = canvas
        self.bind_all("<MouseWheel>", self._on_team_mousewheel)
        self.bind_all("<Button-4>", self._on_team_mousewheel)
        self.bind_all("<Button-5>", self._on_team_mousewheel)

        # Config panel
        cfg = tk.Frame(self, bg="#0b1220", highlightthickness=1, highlightbackground="#1b2a41")
        cfg.pack(fill="x", padx=16, pady=(8,16))

        ttk.Label(cfg, text="Start Grid:", width=12).grid(row=0, column=0, padx=4, pady=6, sticky="e")
        self.start_grid_var = tk.StringVar(value=str(self.start_grid_points))
        self.start_grid_entry = ttk.Entry(cfg, textvariable=self.start_grid_var, width=8)
        self.start_grid_entry.grid(row=0, column=1, padx=4, pady=6, sticky="w")

        ttk.Label(cfg, text="Duration (min):", width=12).grid(row=0, column=2, padx=4, pady=6, sticky="e")
        self.duration_var = tk.StringVar(value=str(int(self.duration_sec/60)))
        self.duration_entry = ttk.Entry(cfg, textvariable=self.duration_var, width=8)
        self.duration_entry.grid(row=0, column=3, padx=4, pady=6, sticky="w")

        ttk.Button(cfg, text="Apply (paused)", command=self.apply_settings).grid(row=0, column=4, padx=10, pady=6)

        # Footer
        footer = tk.Frame(self, bg="#0b1220")
        footer.pack(fill="x")
        self.help_label = tk.Label(
            footer,
            text="Space: Start/Pause • F: Fullscreen • 1/2/3: ½x/1x/2x • S: +2 Station • M: +7 MUS • R: Reset • Esc: Exit Fullscreen",
            font=("Helvetica", 12), fg="#6e85a6", bg="#0b1220"
        )
        self.help_label.pack(pady=(4,10))

        # ttk theme tweaks
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except:
            pass

    def _init_sentiments(self):
        self.sentiment_map = [
            ("tier_0", "#8be9fd", [
                "All lights on and everyone happy.",
                "Downtown fountains glow to the beat of steady power.",
                "Coffee shops brag about 24/7 cold brew chillers."
            ]),
            ("tier_1", "#a8e6ff", [
                "Lights dim slightly; social feeds shrug.",
                "Energy bloggers note a harmless blip in the curve.",
                "Morning news anchors joke about the grid stretching its legs."
            ]),
            ("tier_2", "#bee3ff", [
                "Energy watchers note a minor dip in reserves.",
                "Grid ops tweet charts showing plenty of headroom.",
                "Commuters cheer: 'At least the Wi-Fi still rocks!'"
            ]),
            ("tier_3", "#d4e0ff", [
                "Bloggers start explaining how the grid works.",
                "Utility podcast releases 'Why Transformers Hum' bonus episode.",
                "City trivia night theme: 'Grid Facts to Impress Friends.'"
            ]),
            ("tier_4", "#e6dcff", [
                "Neighbors compare flashlight brands just in case.",
                "Hardware stores promote 'peace-of-mind' lantern bundles.",
                "Local bakery bakes 'Power Puff' cupcakes celebrating uptime."
            ]),
            ("tier_5", "#f4d9ff", [
                "Rumors of voltage drops trend on GridTok.",
                "Group chats trade memes about unplugging air fryers.",
                "Utility mascots post upbeat tips: 'Charge while you can!'"
            ]),
            ("tier_6", "#ffe08a", [
                "Public service alert: conserve power where possible.",
                "City app nudges residents to dial back the A/C.",
                "Community influencers launch #GlowTogether challenges."
            ]),
            ("tier_7", "#ffd166", [
                "Local radio interviews grid analysts for updates.",
                "Morning shows bring on experts to decode the grid.",
                "Mayor livestreams with upbeat 'We've got this' messages."
            ]),
            ("tier_8", "#ffbe5d", [
                "Some blocks report flickers; hotline waits increase.",
                "Neighborhood forums light up with flicker sightings.",
                "Pop-up volunteers hand out phone chargers 'just in case.'"
            ]),
            ("tier_9", "#ffad4d", [
                "Mayor urges calm as crews rebalance supply lines.",
                "City council schedules an emergency energy briefing.",
                "Utility drones film progress shots to reassure residents."
            ]),
            ("tier_10", "#ff9f1c", [
                "Headlines warn of rolling outages if strain continues.",
                "News crawls mention contingency circuits spinning up.",
                "Neighborhood alert sirens test their speakers."
            ]),
            ("tier_11", "#ff7f11", [
                "Civic centers open warming stations preemptively.",
                "Community halls extend hours for device charging.",
                "Emergency texts swap tips on conserving every watt."
            ]),
            ("tier_12", "#ff6600", [
                "Businesses power down signage to help balance demand.",
                "Mall marquees switch to low-power text-only mode.",
                "Transit platforms glow with backup lanterns."
            ]),
            ("tier_13", "#ff4d00", [
                "Transit slows as priority shifts to hospitals.",
                "Metro trims departures to conserve traction power.",
                "Hospitals double-check surge protectors and backup supply."
            ]),
            ("tier_14", "#ff3600", [
                "Neighborhood polls show widespread concern.",
                "Volunteer nets organize check-ins on vulnerable residents.",
                "Emergency radio plays nonstop updates and soothing music."
            ]),
            ("tier_15", "#ff1f00", [
                "Public outages cause widespread concern.",
                "Talk shows debate how the crisis reached this point.",
                "Streetlights flicker while sirens echo across town."
            ]),
        ]
        self.sentiment_high = ("high", "#7fffd4", [
            "Grid hums like a festival—everyone keeps the party lights on.",
            "Power crews stream selfies from the control room with thumbs up.",
            "Kids plan backyard movie nights powered by flawless supply."
        ])
        self.sentiment_low = ("low", "#ff1200", [
            "Sirens blare as neighborhoods plunge into darkness.",
            "Emergency broadcasts plead: unplug everything non-essential now!",
            "Families huddle by lantern light while helicopters scan the skyline."
        ])
        self.sentiment_success = ("success", "#19e68c", [
            "Citywide cheer as the grid saves the day!",
            "Street parades celebrate a victorious power play.",
            "Operators high-five as the control room lights up in green."
        ])
        self.sentiment_failure = ("failure", "#ff1f00", [
            "Public outages cause widespread concern.",
            "Emergency crews battle cascading blackouts across the city.",
            "News helicopters capture whole districts fading to black."
        ])
        self.current_sentiment_key = None
        self.sentiment_index = 0
        self.last_sentiment_update = time.monotonic()

    # ---------- Controls ----------
    def start(self):
        if not self.running:
            self.running = True
            self.status_label.config(text="RUNNING", fg="#19e68c")
            self.alert_state = "none"
            self.clear_victory_effect()
            self.public_label.config(text="All lights on and everyone happy.", fg="#8be9fd")
            self._set_alert_flag("GRID ONLINE", "#19e68c")
            self.current_sentiment_key = None
            self.sentiment_index = 0
            self.last_sentiment_update = time.monotonic()
            self.last_tick_ms = None
            self.start_btn.config(state="disabled")
            self.pause_btn.config(state="normal")

    def pause(self):
        if self.running:
            self.running = False
            self.status_label.config(text="PAUSED", fg="#ffe08a")
            self.start_btn.config(state="normal")
            self.pause_btn.config(state="disabled")

    def toggle_start_pause(self):
        if self.running:
            self.pause()
        else:
            self.start()

    def reset(self):
        self.running = False
        self.grid_points = float(self.start_grid_points)
        self.elapsed_sec = 0.0
        self.rate_multiplier = 1.0
        self.alert_state = "none"
        self.clear_victory_effect()
        self.public_label.config(text="All lights on and everyone happy.", fg="#8be9fd")
        self._set_alert_flag("GRID ONLINE", "#19e68c")
        self.team_station_counts = [0] * 10
        self.team_mus_counts = [0] * 10
        for lbl in self.team_station_labels:
            lbl.config(text="0")
        for lbl in self.team_mus_labels:
            lbl.config(text="0")
        for lbl in self.team_station_points_labels:
            lbl.config(text="0 pts")
        for lbl in self.team_mus_points_labels:
            lbl.config(text="0 pts")
        for lbl in self.team_total_points_labels:
            lbl.config(text="0 pts")
        self._refresh_team_totals()
        self.current_sentiment_key = None
        self.sentiment_index = 0
        self.last_sentiment_update = time.monotonic()
        self.status_label.config(text="PAUSED", fg="#ffe08a")
        self._refresh_labels()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled")

    def set_quarter_rate(self):
        self.rate_multiplier = 0.25
        self._refresh_labels()

    def set_half_rate(self):
        self.rate_multiplier = 0.5
        self._refresh_labels()

    def set_normal_rate(self):
        self.rate_multiplier = 1.0
        self._refresh_labels()

    def set_double_rate(self):
        self.rate_multiplier = 2.0
        self._refresh_labels()

    def set_quad_rate(self):
        self.rate_multiplier = 4.0
        self._refresh_labels()

    def set_paused_rate(self):
        self.rate_multiplier = 0.0
        self._refresh_labels()

    def show_victory_effect(self):
        self.victory_active = True
        message = "⚡⚡⚡ GRID VICTORY! ⚡⚡⚡\n🎈🎈🎈🎈🎈"
        self.victory_label.config(text=message, fg="#19e68c")
        self.public_label.config(text="", fg="#8be9fd")

    def clear_victory_effect(self):
        if not self.victory_active and not self.victory_label.cget("text"):
            return
        self.victory_active = False
        self.victory_label.config(text="", fg="#19e68c")
        self.current_sentiment_key = None
        self.sentiment_index = 0
        self.last_sentiment_update = time.monotonic()
        if not self.running:
            self.public_label.config(text="All lights on and everyone happy.", fg="#8be9fd")
        self._refresh_labels()

    def add_station(self):
        self.add_points(2)

    def add_mus(self):
        self.add_points(7)

    def add_points(self, pts: int):
        self.grid_points = max(0.0, self.grid_points + pts)
        self._refresh_labels()

    def update_team_station(self, team_idx: int, delta: int):
        if not (0 <= team_idx < len(self.team_station_counts)):
            return
        old = self.team_station_counts[team_idx]
        new = max(0, old + delta)
        if new == old:
            return
        self.team_station_counts[team_idx] = new
        self.team_station_labels[team_idx].config(text=str(new))
        self.add_points((new - old) * 2)
        self._refresh_team_totals()

    def update_team_mus(self, team_idx: int, delta: int):
        if not (0 <= team_idx < len(self.team_mus_counts)):
            return
        old = self.team_mus_counts[team_idx]
        new = max(0, old + delta)
        if new == old:
            return
        self.team_mus_counts[team_idx] = new
        self.team_mus_labels[team_idx].config(text=str(new))
        self.add_points((new - old) * 7)
        self._refresh_team_totals()

    def apply_settings(self):
        if self.running:
            messagebox.showinfo("Apply Settings", "Pause the timer to apply settings.")
            return
        try:
            sg = int(self.start_grid_var.get())
            dur_min = int(self.duration_var.get())
            if sg <= 0 or dur_min <= 0:
                raise ValueError
            self.start_grid_points = sg
            self.duration_sec = dur_min * 60
            # Also reset current values to reflect new baseline
            self.reset()
        except ValueError:
            messagebox.showerror("Invalid input", "Enter positive integers for start grid and duration (minutes).")

    def toggle_fullscreen(self):
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    def exit_fullscreen(self):
        self.attributes("-fullscreen", False)

    # ---------- Loop ----------
    def _tick(self):
        # Called ~every 100ms
        now_ms = self.winfo_fpixels('1i')  # hack to force tk to update; we'll use after timing instead
        # Use after-based delta timing
        if self.last_tick_ms is None:
            dt = 0.1
        else:
            dt = 0.1  # Fixed timestep keeps it smooth and consistent for projection

        if self.running and self.grid_points > 0 and self.elapsed_sec < self.duration_sec:
            self.elapsed_sec += dt
            # Depletion
            depletion = self.baseline_rate_pts_per_sec * self.rate_multiplier * dt
            self.grid_points = max(0.0, self.grid_points - depletion)

            # End conditions
            if self.grid_points <= 0.0:
                self.grid_points = 0.0
                self.running = False
                self.alert_state = "failure"
                self.status_label.config(text="BLACKOUT", fg="#ff5c5c")
            elif self.elapsed_sec >= self.duration_sec:
                self.running = False
                if self.grid_points > 0:
                    self.alert_state = "success"
                self.status_label.config(text="TIME!", fg="#91baff")

            self._refresh_labels()

        self.last_tick_ms = (self.last_tick_ms or 0) + int(dt * 1000)
        self.after(100, self._tick)

    # ---------- Helpers ----------
    def _on_team_mousewheel(self, event):
        if not self._team_scroll_canvas or not self.team_container:
            return

        widget_under_pointer = self.winfo_containing(event.x_root, event.y_root)
        if widget_under_pointer is None:
            return

        current = widget_under_pointer
        inside_tracker = False
        while current is not None:
            if current == self.team_container:
                inside_tracker = True
                break
            current = getattr(current, "master", None)

        if not inside_tracker:
            return

        direction = 0
        steps = 1

        delta = getattr(event, "delta", 0)
        if delta:
            direction = -1 if delta > 0 else 1 if delta < 0 else 0
            magnitude = abs(delta)
            steps = max(1, int(magnitude / 120)) if magnitude >= 120 else 1
        else:
            num = getattr(event, "num", None)
            if num == 4:
                direction = -1
            elif num == 5:
                direction = 1

        if direction != 0:
            self._team_scroll_canvas.yview_scroll(direction * steps, "units")
            return "break"

    def _set_alert_flag(self, text, color, flicker_rate=None):
        signature = (text, color, flicker_rate)
        if self.current_alert_signature == signature:
            return
        self.current_alert_signature = signature
        if self.alert_flicker_job:
            self.after_cancel(self.alert_flicker_job)
            self.alert_flicker_job = None
        self.alert_flag_visible_color = color
        self.alert_flicker_rate = flicker_rate
        self.alert_flicker_state = True
        self.alert_flag.config(text=text, fg=color)
        if flicker_rate:
            self.alert_flicker_job = self.after(flicker_rate, self._toggle_alert_flicker)

    def _toggle_alert_flicker(self):
        if self.alert_flicker_rate is None:
            self.alert_flicker_job = None
            self.alert_flag.config(fg=self.alert_flag_visible_color)
            return
        self.alert_flicker_state = not self.alert_flicker_state
        fg = self.alert_flag_visible_color if self.alert_flicker_state else "#1b2a41"
        self.alert_flag.config(fg=fg)
        self.alert_flicker_job = self.after(self.alert_flicker_rate, self._toggle_alert_flicker)

    def _refresh_team_totals(self):
        total_station = 0
        total_mus = 0
        for idx, (station_count, mus_count) in enumerate(zip(self.team_station_counts, self.team_mus_counts)):
            station_points = station_count * 2
            mus_points = mus_count * 7
            team_total = station_points + mus_points

            if idx < len(self.team_station_labels):
                self.team_station_labels[idx].config(text=str(station_count))
            if idx < len(self.team_mus_labels):
                self.team_mus_labels[idx].config(text=str(mus_count))
            if idx < len(self.team_station_points_labels):
                self.team_station_points_labels[idx].config(text=f"{station_points} pts")
            if idx < len(self.team_mus_points_labels):
                self.team_mus_points_labels[idx].config(text=f"{mus_points} pts")
            if idx < len(self.team_total_points_labels):
                self.team_total_points_labels[idx].config(text=f"{team_total} pts")

            total_station += station_count
            total_mus += mus_count

        station_points_total = total_station * 2
        mus_points_total = total_mus * 7
        total_points = station_points_total + mus_points_total

        self.total_station_count_label.config(text=str(total_station))
        self.total_station_points_label.config(text=f"{station_points_total} pts")
        self.total_mus_count_label.config(text=str(total_mus))
        self.total_mus_points_label.config(text=f"{mus_points_total} pts")
        self.total_points_label.config(text=f"{total_points} pts")
        self.total_points_detail_label.config(text=f"{total_points} pts")

    def _refresh_labels(self):
        gp_display = max(0.0, self.grid_points)
        gp_int = int(round(gp_display))
        # Color shift when low
        if gp_display <= 5:
            color = "#ff5c5c"
        elif gp_display <= 10:
            color = "#ffd166"
        else:
            color = "#19e68c"
        self.grid_label.config(text=f"{gp_display:.3f}", fg=color)
        if gp_display <= 0:
            self.alert_state = "failure"
            self._set_alert_flag("GRID FAILURE", "#ff5c5c")
        elif self.alert_state == "success":
            self._set_alert_flag("GRID SECURE", "#19e68c")
        else:
            if self.alert_state == "failure":
                self.alert_state = "none"
            if gp_display <= 5:
                self._set_alert_flag("GRID ONLINE", "#ff5c5c", flicker_rate=400)
            elif gp_display <= 10:
                self._set_alert_flag("GRID ONLINE", "#ffd166", flicker_rate=800)
            else:
                self._set_alert_flag("GRID ONLINE", "#19e68c")

        remaining = max(0, int(round(self.duration_sec - self.elapsed_sec)))
        self.timer_label.config(text=self._fmt_time(remaining))

        self.rate_label.config(text=self._rate_text())
        if not self.victory_active:
            self._update_public_opinion(gp_display)

    def _fmt_time(self, seconds):
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    def _rate_text(self):
        if not self.running and self.grid_points <= 0:
            return "Depletion: 0 (BLACKOUT)"
        rate_pt_s = self.baseline_rate_pts_per_sec * self.rate_multiplier
        # Show human readable summary
        if rate_pt_s == 0:
            per = "∞"
        else:
            per = f"{1.0/rate_pt_s:.1f}s per point"
        mult = {
            0.0: "0×",
            0.25: "¼×",
            0.5: "½×",
            1.0: "1×",
            2.0: "2×",
            4.0: "4×",
        }.get(self.rate_multiplier, f"{self.rate_multiplier:.2f}×")
        return f"Depletion: {mult}  •  {rate_pt_s:.3f} pts/s  •  ~{per}"

    def _update_public_opinion(self, gp_display: float):
        capacity = max(1, int(round(self.start_grid_points)))

        if self.alert_state == "failure":
            key, color, messages = self.sentiment_failure
        elif self.alert_state == "success":
            key, color, messages = self.sentiment_success
        else:
            if gp_display >= 10:
                key, color, messages = self.sentiment_high
            elif gp_display <= 5:
                key, color, messages = self.sentiment_low
            else:
                gp_int = int(round(gp_display))
                drop = max(0, capacity - min(gp_int, capacity))
                max_tier = len(self.sentiment_map) - 1
                tier = max(0, min(max_tier, drop))
                key, color, messages = self.sentiment_map[tier]

        if key != self.current_sentiment_key:
            self.current_sentiment_key = key
            self.sentiment_index = 0
            self.last_sentiment_update = time.monotonic()
        else:
            now = time.monotonic()
            if messages and len(messages) > 1 and now - self.last_sentiment_update >= 10:
                self.sentiment_index = (self.sentiment_index + 1) % len(messages)
                self.last_sentiment_update = now

        if messages:
            text = messages[self.sentiment_index % len(messages)]
        else:
            text = ""

        self.public_label.config(text=text, fg=color)

if __name__ == "__main__":
    app = GridApp()
    app.mainloop()
