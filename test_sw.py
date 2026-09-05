"""Print installed Mininet switch implementation when run explicitly."""
if __name__ == "__main__":
    import inspect
    from mininet.node import OVSSwitch
    print(inspect.getsource(OVSSwitch.start))
