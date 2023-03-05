import aprs

with aprs.TCP(
    host="localhost",
    port=14588,
    user="KF7HVM",
    passcode="-1",  # use a real passcode for TX
    command='filter r/46.1/-122.9/500',
) as aprs_tcp:
    # block until 1 frame is available and print repr
    while frames := aprs_tcp.read(min_frames=1):
        for f in frames:
            print(f)